#!/usr/bin/env python3
"""T13c只读探测27候选成分、同花顺/新浪上市日期，所有证据只放logs/test。"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import urllib.parse

import requests

from earnings_universe import (BJ, COMPONENT_REFERENCE, SINA_COMPANY, SW_COMPONENT,
                               THS_COMPANY, candidate_industries, component_page,
                               current_legacy_members, listing_date)
from flow_source import verify_tls

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ['000001', '600519', '300761', '001267', '688981', '600309', '600019', '600048', '601127']


class ProbeHTTP:
    def __init__(self, end, bundle, directory, interval=1):
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise RuntimeError('金融请求只允许GitHub Actions，本机只能离线解析保存的响应')
        self.end, self.bundle, self.directory, self.interval = end, str(bundle), Path(directory), interval
        self.last = 0
        self.session = requests.Session()
        self.trace = []

    def get(self, url, key):
        allowed = url.startswith(SW_COMPONENT + '?') or any(url == template.format(code=code)
                    for template in (THS_COMPANY, SINA_COMPANY) for code in SAMPLES)
        if not allowed:
            raise ValueError('探测地址不在明确的成分/公司页集合')
        wait = max(0, self.last + self.interval - time.monotonic())
        if time.monotonic() + wait + 1 >= self.end:
            raise TimeoutError('探测总体硬预算已到')
        time.sleep(wait)
        self.last = time.monotonic()
        def expired(*_):
            raise TimeoutError('请求墙钟超时')
        previous = signal.signal(signal.SIGALRM, expired)
        signal.alarm(min(40, max(1, int(self.end - time.monotonic()))))
        entry = {'url': url}
        try:
            referer = 'https://www.swsresearch.com/' if url.startswith(SW_COMPONENT) else 'https://basic.10jqka.com.cn/' if '10jqka' in url else 'https://finance.sina.com.cn/'
            response = self.session.get(url, timeout=(10, 20), verify=self.bundle if url.startswith(SW_COMPONENT) else True,
                                        headers={'User-Agent': 'Mozilla/5.0', 'Referer': referer})
            response.encoding = response.apparent_encoding or 'utf-8'
            raw = response.content
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (key + '.bin')
            path.write_bytes(raw)
            entry.update({'实际URL': response.url, 'HTTP': response.status_code,
                          '原响应': str(path.relative_to(self.directory.parent)), '编码': response.encoding,
                          '字节': len(raw), 'SHA256': hashlib.sha256(raw).hexdigest()})
            final = urllib.parse.urlsplit(response.url)
            if final.scheme != 'https' or final.hostname != urllib.parse.urlsplit(url).hostname:
                raise ValueError('来源重定向改变HTTPS或域名，拒绝沿用来源身份')
            response.raise_for_status()
            return response.text
        except (requests.RequestException, TimeoutError, ValueError) as exc:
            entry['错误'] = type(exc).__name__ + ': ' + str(exc)[:300]
            raise
        finally:
            self.trace.append(entry)
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)


def probe(root=ROOT, *, budget=900, client=None):
    root = Path(root)
    if not 60 <= budget <= 1800:
        raise ValueError('探测总体预算须60..1800秒')
    stamp = dt.datetime.now(BJ).isoformat()
    out = root / 'logs/test/T13c/探测' / stamp.replace(':', '').replace('+', '_')
    out.mkdir(parents=True, exist_ok=True)
    names = candidate_industries(root)
    legacy = current_legacy_members(root)
    report = {'生成时间': stamp, '来源分支': os.environ.get('GITHUB_REF_NAME', '离线测试'),
              '来源SHA': os.environ.get('GITHUB_SHA', '离线测试'), '来源运行': os.environ.get('GITHUB_RUN_ID', '离线测试'),
              '成分实现依据': COMPONENT_REFERENCE, '候选行业': names,
              '旧快照覆盖候选数': len(set(names) & set(legacy)), '实际缺失候选': {c: names[c] for c in names if c not in legacy},
              '成分': {}, '上市日期': [], '错误': [], '正式数据未写入': True}
    deadline = time.monotonic() + budget
    if client is None:
        bundle, tls = verify_tls(root, out)
        report['TLS'] = tls
        client = ProbeHTTP(deadline, bundle, out / '原响应')
    def save():
        report['请求'] = client.trace
        (out / '结果.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    memberships = {}
    for code, name in names.items():
        try:
            rows, total, page = [], None, 1
            while True:
                if time.monotonic() + 1 >= deadline:
                    raise TimeoutError('探测总体硬预算已到')
                url = SW_COMPONENT + '?' + urllib.parse.urlencode({'swindexcode': code, 'page': page, 'page_size': 1000})
                text = client.get(url, '申万_' + code + '_' + str(page))
                current, count = component_page(json.loads(text))
                if total is not None and total != count:
                    raise ValueError('翻页时成分总数变化')
                total = count
                if not current:
                    raise ValueError('分页未完整时返回空页')
                rows.extend(current)
                if len({r['证券代码'] for r in rows}) != len(rows) or len(rows) > total:
                    raise ValueError('成分跨页重复或超过总数')
                if len(rows) == total:
                    break
                page += 1
                if page > 100:
                    raise ValueError('成分页数超过硬上限')
            report['成分'][code] = {'行业': name, '完整总数': total, '页数': page, 'rows': rows,
                                      '快照日期': stamp[:10], '来源': SW_COMPONENT}
            for row in rows:
                memberships.setdefault(row['证券代码'], []).append(code)
        except (requests.RequestException, ValueError, KeyError, TypeError, TimeoutError) as exc:
            report['错误'].append({'行业': code, '错误': type(exc).__name__ + ': ' + str(exc)[:300]})
        save()
    report['跨行业重复'] = {c: sectors for c, sectors in memberships.items() if len(sectors) > 1}
    if report['跨行业重复']:
        report['错误'].append({'错误': '完整候选成分存在跨行业重复，不选择性丢弃'})
    for code in SAMPLES:
        result = {'代码': code, '行业': memberships.get(code, []), '尝试': []}
        for source, label in ((THS_COMPANY.format(code=code), '同花顺'), (SINA_COMPANY.format(code=code), '新浪')):
            try:
                text = client.get(source, label + '_' + code)
                result.update(listing_date(text, code, source))
                result['状态'] = '成功'
                break
            except (requests.RequestException, ValueError, KeyError, TypeError, TimeoutError) as exc:
                result['尝试'].append({'来源': source, '错误': type(exc).__name__ + ': ' + str(exc)[:300]})
        if result.get('状态') != '成功':
            result['状态'] = '未知，不进入分母'
        report['上市日期'].append(result)
        save()
    successful = sum(r['状态'] == '成功' for r in report['上市日期'])
    report['独立股票数'] = len(memberships)
    report['完整候选数'] = len(report['成分'])
    report['成功上市日期样本数'] = successful
    report['状态'] = '探测通过' if not report['错误'] and len(report['成分']) == 27 and successful >= 6 else '探测失败'
    report['说明'] = '指数计入日期不是上市日期；公司成立日期不可替代。当前成分快照回溯仍有幸存者偏差。'
    save()
    print(json.dumps({k: report[k] for k in ('状态', '完整候选数', '独立股票数', '成功上市日期样本数', '错误')}, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--budget', type=int, default=900)
    args = parser.parse_args()
    raise SystemExit(0 if probe(args.root, budget=args.budget)['状态'] == '探测通过' else 1)
