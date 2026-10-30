#!/usr/bin/env python3
"""T13b 同花顺单季历史：Actions 限时采集、逐股断点、原值只追加。"""
import argparse
import ast
import csv
import datetime as dt
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import signal
import time

import requests

from flow_source import append_rows

ROOT = Path(__file__).resolve().parents[1]
BJ = dt.timezone(dt.timedelta(hours=8))
SOURCE_CODE = 'https://github.com/akfamily/akshare/blob/5def33b2d0ba51529e386cb86d7da0c85e5a5626/akshare/stock/finance/stock_finance_ths.py'
URL = 'https://basic.10jqka.com.cn/new/{code}/finance.html'
FIELDS = ['报告期', '扣非净利润', '净利润', '营业总收入', '利润口径', '法定截止', '可见时刻', '来源', '采集时间', '修订原因']
SOURCE_VERSION = '同花顺main.simple-v2'


def load_v3(root):
    """只编译已有函数，不导入会建缓存/联网的旧选股模块。"""
    path = Path(root) / 'scripts/v3_select.py'
    body, cfg = [], {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in ('num', 'q_signal'):
            body.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CFG' for t in node.targets):
            cfg = ast.literal_eval(node.value)
    namespace = {'dt': dt, 'math': math, 're': re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), namespace)
    if not {'num', 'q_signal'}.issubset(namespace):
        raise ValueError('未找到原v3季度函数')
    config = Path(root) / 'data/v3/config.json'
    if config.exists():
        cfg.update(json.loads(config.read_text()))
    return namespace['num'], namespace['q_signal'], cfg


def deadline(period):
    d = dt.date.fromisoformat(period)
    if (d.month, d.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        raise ValueError('非标准季度末：' + period)
    month, day = {3: (4, 30), 6: (8, 31), 9: (10, 31), 12: (4, 30)}[d.month]
    return dt.date(d.year + (d.month == 12), month, day)


def visibility(period):
    return (deadline(period) + dt.timedelta(days=1)).isoformat() + 'T00:00:00+08:00'


def members(root):
    path = Path(root) / 'data/v3/申万成分.json'
    obj = json.loads(path.read_text())
    codes = set()
    for industry, entry in obj.items():
        if not entry.get('date') or not entry.get('rows'):
            raise ValueError('行业成分快照不完整：' + industry)
        dt.date.fromisoformat(entry['date'])
        for row in entry['rows']:
            code = str(row['证券代码']).zfill(6)
            if not re.fullmatch(r'\d{6}', code):
                raise ValueError('无效成分代码：' + code)
            codes.add(code)
    return obj, sorted(codes), hashlib.sha256(path.read_bytes()).hexdigest()


class MainParagraph(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.parts = []
        self.found = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'p' and dict(attrs).get('id') == 'main':
            self.inside = True
            self.found += 1

    def handle_endtag(self, tag):
        if tag == 'p':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


RAW_FIELDS = [f for f in FIELDS if f not in ('采集时间', '修订原因')]
MISSING_AMOUNTS = {'', '--', 'nan', 'none', 'false'}


def validate_amount(value, num):
    if not isinstance(value,str):
        raise ValueError('财务原值必须为字符串')
    parsed=num(value)
    if not math.isfinite(parsed) and not (math.isnan(parsed) and value.strip().lower() in MISSING_AMOUNTS):
        raise ValueError('财务原值非有限数字或已知缺值标记：'+value[:60])


def rows_digest(rows):
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def capture_path(stamp, code):
    run_stamp=stamp.replace(':','').replace('+','_')
    return Path('logs/test/T13b/抓取响应')/run_stamp/(code+'.html')

def parse_page(text, code, num, start='2013-01-01'):
    """照官方 stock_financial_abstract_ths 的 title/simple 转置，不拼造接口。"""
    parser = MainParagraph()
    parser.feed(text)
    if parser.found != 1:
        raise ValueError('缺少唯一main财报节点；可能页面改版或反爬，不能当无历史')
    obj = json.loads(''.join(parser.parts))
    if not isinstance(obj, dict) or not isinstance(obj.get('title'), list) or not isinstance(obj.get('simple'), list):
        raise ValueError('main缺title/simple数组')
    labels = [x[0] if isinstance(x, list) and x else x for x in obj['title']]
    matrix = obj['simple']
    if not matrix or len(matrix) != len(labels):
        raise ValueError('title/simple行数不一致')
    labels = labels[1:]
    if len(set(labels)) != len(labels) or not {'净利润', '营业总收入'}.issubset(labels):
        raise ValueError('关键财务字段缺失或重复')
    dates = matrix[0]
    if not isinstance(dates, list) or len(set(dates)) != len(dates):
        raise ValueError('季度列无效或重复')
    if any(not isinstance(x, list) or len(x) != len(dates) for x in matrix[1:]):
        raise ValueError('simple矩阵列数不一致')
    result = []
    for i, period in enumerate(dates):
        if not isinstance(period, str):
            raise ValueError('报告期非字符串')
        due = deadline(period)
        if period < start:
            continue
        values = dict(zip(labels, (r[i] for r in matrix[1:])))
        def raw(key):
            v = values.get(key)
            # 同花顺真实页面用JSON false表示缺值；沿v3保留False标记，不能当0。
            if v is False:
                return 'False'
            if isinstance(v, (dict, list, bool)):
                raise ValueError('财务原值类型无效：' + key)
            return '' if v is None else str(v)
        kf, profit, revenue = (raw(k) for k in ('扣非净利润', '净利润', '营业总收入'))
        for value in (kf,profit,revenue):validate_amount(value,num)
        basis = '扣非净利润' if num(kf)==num(kf) else '净利润回退' if num(profit)==num(profit) else '缺失'
        result.append({'报告期': period, '扣非净利润': kf, '净利润': profit, '营业总收入': revenue,
                       '利润口径': basis, '法定截止': due.isoformat(), '可见时刻': visibility(period), '来源': URL.format(code=code)})
    return sorted(result, key=lambda r: r['报告期'])


def journal(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')


def read_status(path):
    records = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                records[row['代码']] = row
    return records


class Client:
    def __init__(self, end, interval=1.0):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise RuntimeError('真实财经请求只允许GitHub Actions')
        self.end, self.interval, self.last = end, interval, 0.0
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://basic.10jqka.com.cn/'})

    def get(self, code):
        wait = max(0, self.last + self.interval - time.monotonic())
        if time.monotonic() + wait + 2 >= self.end:
            raise TimeoutError('采集硬预算已到')
        time.sleep(wait)
        self.last = time.monotonic()
        # requests的read timeout不是总耗时；SIGALRM另限请求墙钟，含DNS和慢速响应。
        seconds = min(40, max(1, int(self.end - time.monotonic())))
        def expired(*_):
            raise TimeoutError('请求墙钟超时')
        previous = signal.signal(signal.SIGALRM, expired)
        signal.alarm(seconds)
        try:
            response = self.session.get(URL.format(code=code), timeout=(20, 30))
            response.raise_for_status()
            response.encoding = response.apparent_encoding or 'utf-8'
            return response.text
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)


def probe_ready(root):
    from earnings_history_publish import validate_probe_evidence
    for path in (Path(root)/'logs/test/T13b').glob('发布_*.json'):
        try:
            validate_probe_evidence(Path(root),json.loads(path.read_text()))
            return
        except (ValueError, OSError, KeyError, TypeError):
            continue
    raise ValueError('批量前需发布同版本至少3只不同股票、原始HTML与重解析一致的探测证据')


def collect(root=ROOT, *, mode='probe', codes=None, max_stocks=400, budget=2700, refresh=False, client=None):
    if not 1 <= budget <= 3000 or not 1 <= max_stocks <= 2500:
        raise ValueError('预算须1..3000秒，单批家数须1..2500')
    root = Path(root)
    snapshot, universe, digest = members(root)
    selected = codes if codes is not None else (['000063', '600519', '000001'] if mode == 'probe' else universe)
    if not selected or any(not re.fullmatch(r'\d{6}', c) for c in selected):
        raise ValueError('股票代码必须为6位数字')
    if mode == 'batch' and not set(selected).issubset(universe):
        raise ValueError('批量股票必须属于当前申万成分快照')
    end = time.monotonic() + budget
    if client is None and mode=='batch':probe_ready(root)
    client = client or Client(end)
    num, _, _ = load_v3(root)
    base = root / 'data/财报/全市场单季'
    work = root / 'logs/test/T13b'
    work.mkdir(parents=True, exist_ok=True)
    state_path = base / '采集状态.jsonl'
    previous = read_status(state_path) if mode == 'batch' else {}
    stamp = dt.datetime.now(BJ).isoformat()
    run_id = stamp.replace(':', '').replace('+', '_')
    results, skipped, collected_rows, captures = [], 0, {}, {}
    for code in selected:
        prior = previous.get(code, {})
        if not refresh and prior.get('状态') in ('成功', '无2013起历史') and prior.get('来源版本') == SOURCE_VERSION and prior.get('成分SHA256') == digest:
            if prior['状态'] == '无2013起历史' or (base / (code + '.csv')).exists():
                skipped += 1
                continue
        if len(results) >= max_stocks or time.monotonic() + 2 >= end:
            break
        record = {'代码': code, '采集时间': stamp, '来源版本': SOURCE_VERSION, '成分SHA256': digest, '来源': URL.format(code=code)}
        try:
            text = client.get(code)
            if mode == 'probe':
                capture = root/capture_path(stamp,code)
                capture.parent.mkdir(parents=True,exist_ok=True)
                capture.write_text(text)
                record['响应SHA256'] = hashlib.sha256(capture.read_bytes()).hexdigest()
                record['响应文件'] = str(capture.relative_to(root))
                captures[code]=text
            rows = parse_page(text, code, num)
            collected_rows[code]=rows
            if mode=='batch':
                record['追加行'] = append_rows(base / (code + '.csv'), FIELDS, rows, ['报告期'], stamp)
            record.update({'状态': '成功' if rows else '无2013起历史', '行数': len(rows),
                           '起点': rows[0]['报告期'] if rows else '', '终点': rows[-1]['报告期'] if rows else '',
                           '净利润回退行数': sum(r['利润口径'] == '净利润回退' for r in rows), '载荷SHA256':rows_digest(rows)})
        except (requests.RequestException, TimeoutError, ValueError, KeyError, TypeError) as exc:
            record.update({'状态': '失败', '错误': type(exc).__name__ + ': ' + str(exc)[:250]})
        results.append(record)
        if mode == 'batch':
            journal(state_path, record)
        print(code + ' ' + record['状态'] + ' ' + str(record.get('行数', '')), flush=True)
    report = {'模式': mode, '生成时间': stamp, '源代码依据': SOURCE_CODE, '来源版本': SOURCE_VERSION, '成分SHA256': digest,
              '快照行业数': len(snapshot), '成分家数': len(universe), '本批尝试': len(results), '断点跳过': skipped,
              '失败数': sum(r['状态'] == '失败' for r in results), '预算秒': budget, '结果': results,
              '说明': '有效空数组/仅2013前历史才记无历史；不能据此断言未上市。当前成分快照回溯有幸存者偏差。'}
    report['来源分支']=os.environ.get('GITHUB_REF_NAME','离线测试')
    report['来源SHA']=os.environ.get('GITHUB_SHA','离线测试')
    report['来源运行']=os.environ.get('GITHUB_RUN_ID','离线测试')
    report['解析器SHA256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    artifact=work/'产物'/run_id;artifact.mkdir(parents=True,exist_ok=True)
    (artifact/'artifact.json').write_text(json.dumps({'报告':report,'单季记录':collected_rows,'探测响应':captures},ensure_ascii=False,indent=2)+'\n')
    (work / ('采集_' + mode + '_' + run_id + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--mode', choices=['probe', 'batch'], default='probe')
    parser.add_argument('--codes', help='逗号分隔；未指定则探测3只/批量全部当前成分')
    parser.add_argument('--max-stocks', type=int, default=400)
    parser.add_argument('--budget', type=int, default=2700)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    report = collect(args.root, mode=args.mode, codes=args.codes.split(',') if args.codes else None,
                     max_stocks=args.max_stocks, budget=args.budget, refresh=args.refresh)
    return 1 if report['失败数'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
