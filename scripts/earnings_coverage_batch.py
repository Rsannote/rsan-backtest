#!/usr/bin/env python3
"""T13c 正式批次：从已验证探测产物取 27 行业，只在 Actions 抓上市日和缺失财报。"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time
import urllib.parse

import requests

from earnings_history import (BJ, RAW_FIELDS, SOURCE_CODE, SOURCE_VERSION, URL,
                              load_v3, parse_page, rows_digest)
from earnings_universe import SINA_COMPANY, SW_COMPONENT, listing_date

ROOT = Path(__file__).resolve().parents[1]
LISTING_VERSION = '同花顺-新浪公司页上市日-v1'


def probe_universe(result):
    """探测结果是正式批次唯一成分输入；拒绝部分成功和跨行业重复。"""
    if result.get('状态') != '探测通过' or result.get('正式数据未写入') is not True:
        raise ValueError('成分探测未通过或已被改造成正式结果')
    if result.get('完整候选数') != 27 or result.get('错误') or result.get('跨行业重复'):
        raise ValueError('成分探测不是无错误的 27 行业完整快照')
    industries = result.get('成分')
    if not isinstance(industries, dict) or len(industries) != 27:
        raise ValueError('成分探测行业集合不完整')
    seen = set()
    business = {}
    for sector, entry in industries.items():
        if entry.get('来源') != SW_COMPONENT or entry.get('完整总数') != len(entry.get('rows', [])):
            raise ValueError('成分来源或总数不一致')
        codes = []
        for row in entry['rows']:
            code = str(row.get('证券代码', '')).zfill(6)
            if not re.fullmatch(r'\d{6}', code) or code in seen:
                raise ValueError('成分代码非法或跨行业重复')
            seen.add(code); codes.append(code)
        business[sector] = tuple(sorted(codes))
    if len(seen) != result.get('独立股票数'):
        raise ValueError('成分独立股票数与探测报告不一致')
    digest = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()
    return sorted(seen), digest


class HTTP:
    def __init__(self, end):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise RuntimeError('金融请求只允许 GitHub Actions，本机只能离线测试')
        self.end = end
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})

    def get(self, url):
        if not (url.startswith('https://basic.10jqka.com.cn/new/') or
                url.startswith('https://vip.stock.finance.sina.com.cn/corp/')):
            raise ValueError('请求地址不在已批准的上市日/财报来源')
        if time.monotonic() + 2 >= self.end:
            raise TimeoutError('批次总体硬预算已到')
        seconds = min(40, max(1, int(self.end - time.monotonic())))
        previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('请求墙钟超时')))
        signal.alarm(seconds)
        try:
            response = self.session.get(url, timeout=(10, 25), allow_redirects=True,
                                        headers={'Referer': 'https://finance.sina.com.cn/' if 'sina.com' in url else 'https://basic.10jqka.com.cn/'})
            final = urllib.parse.urlsplit(response.url)
            expected = urllib.parse.urlsplit(url)
            if final.scheme != 'https' or final.hostname != expected.hostname:
                raise ValueError('来源重定向改变 HTTPS 或域名')
            response.raise_for_status()
            raw = response.content
            response.encoding = response.apparent_encoding or 'utf-8'
            return response.text, raw
        finally:
            signal.alarm(0); signal.signal(signal.SIGALRM, previous)


def collect(root, probe_result, shard, shards, budget=3000, client=None, resume_paths=()):
    root = Path(root); probe_result = Path(probe_result)
    if not 0 <= shard < shards <= 16 or not 60 <= budget <= 3300:
        raise ValueError('分片或预算越界')
    raw_probe = probe_result.read_bytes(); result = json.loads(raw_probe)
    universe, universe_digest = probe_universe(result)
    selected = [code for index, code in enumerate(universe) if index % shards == shard]
    stamp = dt.datetime.now(BJ).isoformat(); end = time.monotonic() + budget
    client = client or HTTP(end); num, _, _ = load_v3(root)
    existing = {p.stem for p in (root / 'data/财报/全市场单季').glob('*.csv')}
    resumed_listing, resumed_finance, resumed_rows = {}, {}, {}
    for path in resume_paths:
        try:
            prior = json.loads(Path(path).read_text())
            if (prior.get('探测运行') != str(result.get('来源运行')) or prior.get('探测SHA') != result.get('来源SHA') or
                    prior.get('成分业务SHA256') != universe_digest or prior.get('分片') != shard or prior.get('分片总数') != shards):
                continue
            for record in prior.get('上市日期', []):
                if record.get('状态') == '成功': resumed_listing[record['代码']] = record
            for record in prior.get('财报', []):
                if record.get('状态') in ('成功', '无2013起历史'):
                    payload = prior.get('单季记录', {}).get(record['代码'])
                    if isinstance(payload, list):
                        resumed_finance[record['代码']] = record; resumed_rows[record['代码']] = payload
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    listings, financials, rows_by_code = [], [], {}
    for index, code in enumerate(selected, 1):
        listing = resumed_listing.get(code)
        if listing is None:
            listing = {'代码': code, '采集时间': stamp, '实现版本': LISTING_VERSION,
                       '来源': SINA_COMPANY.format(code=code)}
            try:
                text, raw = client.get(listing['来源'])
                listing.update(listing_date(text, code, listing['来源']))
                listing.update({'状态': '成功', '响应SHA256': hashlib.sha256(raw).hexdigest(), '响应字节': len(raw)})
            except (requests.RequestException, TimeoutError, ValueError, KeyError, TypeError) as exc:
                listing.update({'状态': '未知，不进入分母', '上市日期': '',
                                '错误': type(exc).__name__ + ': ' + str(exc)[:250]})
        listings.append(listing)
        if code in existing:
            financials.append({'代码': code, '状态': '复用已发布原始财报', '采集时间': stamp,
                               '来源版本': SOURCE_VERSION})
        elif code in resumed_finance:
            financials.append(resumed_finance[code]); rows_by_code[code] = resumed_rows[code]
        else:
            record = {'代码': code, '采集时间': stamp, '来源版本': SOURCE_VERSION,
                      '来源': URL.format(code=code)}
            try:
                text, raw = client.get(record['来源'])
                rows = parse_page(text, code, num)
                rows_by_code[code] = rows
                record.update({'状态': '成功' if rows else '无2013起历史', '行数': len(rows),
                               '起点': rows[0]['报告期'] if rows else '', '终点': rows[-1]['报告期'] if rows else '',
                               '载荷SHA256': rows_digest(rows), '响应SHA256': hashlib.sha256(raw).hexdigest(),
                               '净利润回退行数': sum(r['利润口径'] == '净利润回退' for r in rows)})
            except (requests.RequestException, TimeoutError, ValueError, KeyError, TypeError) as exc:
                record.update({'状态': '失败', '错误': type(exc).__name__ + ': ' + str(exc)[:250]})
            financials.append(record)
        if index % 100 == 0:
            print(f'分片 {shard}/{shards} {index}/{len(selected)}', flush=True)
    report = {'生成时间': stamp, '来源分支': os.environ.get('GITHUB_REF_NAME', '离线测试'),
              '来源SHA': os.environ.get('GITHUB_SHA', '离线测试'), '来源运行': os.environ.get('GITHUB_RUN_ID', '离线测试'),
              '探测运行': str(result.get('来源运行')), '探测SHA': result.get('来源SHA'),
              '探测结果SHA256': hashlib.sha256(raw_probe).hexdigest(), '成分业务SHA256': universe_digest,
              '成分家数': len(universe), '分片': shard, '分片总数': shards, '代码': selected,
              '上市日期': listings, '财报': financials, '单季记录': rows_by_code,
              '财报实现依据': SOURCE_CODE, '财报来源版本': SOURCE_VERSION,
              '失败数': sum(r['状态'] == '失败' for r in financials),
              '上市日未知数': sum(r['状态'] != '成功' for r in listings),
              '断点复用上市日': len(resumed_listing), '断点复用财报': len(resumed_finance),
              '说明': '当前成分快照回溯有幸存者偏差；上市日未知不进入季度覆盖分母，不用指数计入日或首条行情推测。'}
    output = root / 'logs/test/T13c/批次产物' / (str(shard) + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'分片': shard, '代码': len(selected), '财报失败': report['失败数'],
                      '上市日未知': report['上市日未知数']}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--probe-result', type=Path, required=True)
    parser.add_argument('--shard', type=int, required=True); parser.add_argument('--shards', type=int, default=4)
    parser.add_argument('--budget', type=int, default=3000)
    parser.add_argument('--resume-dir', type=Path)
    args = parser.parse_args()
    resume = sorted(args.resume_dir.rglob('*.json')) if args.resume_dir and args.resume_dir.exists() else []
    report = collect(args.root, args.probe_result, args.shard, args.shards, args.budget, resume_paths=resume)
    return 1 if report['失败数'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
