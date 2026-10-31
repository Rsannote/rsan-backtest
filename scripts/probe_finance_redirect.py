#!/usr/bin/env python3
"""T13c 同花顺财报重定向只读诊断；不保存页面正文，不发布正式数据。"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import urllib.parse

import requests

from earnings_history import BJ, URL, load_v3, parse_page

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CODES = ('000002', '600519', '001267')


def provider_host(host):
    host = (host or '').lower().rstrip('.')
    return host == '10jqka.com.cn' or host.endswith('.10jqka.com.cn')


def public_url(url):
    value = urllib.parse.urlsplit(url)
    return {'scheme': value.scheme.lower(), 'host': (value.hostname or '').lower(),
            'path': value.path, 'has_query': bool(value.query)}


def trace(session, code, *, max_hops=5):
    """只跟随同花顺 HTTPS；HTTP Location 仅尝试等价 HTTPS 升级。"""
    current = URL.format(code=code)
    hops = []
    upgraded = False
    for _ in range(max_hops + 1):
        response = session.get(current, timeout=(10, 25), allow_redirects=False,
                               headers={'User-Agent': 'Mozilla/5.0',
                                        'Referer': 'https://basic.10jqka.com.cn/'})
        item = {'request': public_url(current), 'status': response.status_code}
        location = response.headers.get('Location')
        if location:
            target = urllib.parse.urljoin(current, location)
            target_parts = urllib.parse.urlsplit(target)
            item['location'] = public_url(target)
            hops.append(item)
            if not provider_host(target_parts.hostname):
                return {'代码': code, '状态': '拒绝跨提供方重定向', 'hops': hops}
            if target_parts.scheme.lower() == 'http':
                target = urllib.parse.urlunsplit(('https', target_parts.netloc,
                                                  target_parts.path, target_parts.query,
                                                  target_parts.fragment))
                item['尝试HTTPS升级'] = public_url(target)
                upgraded = True
            elif target_parts.scheme.lower() != 'https':
                return {'代码': code, '状态': '拒绝非HTTP协议重定向', 'hops': hops}
            current = target
            continue
        hops.append(item)
        try:
            response.raise_for_status()
            raw = response.content
            response.encoding = response.apparent_encoding or 'utf-8'
            num, _, _ = load_v3(ROOT)
            rows = parse_page(response.text, code, num)
            return {'代码': code, '状态': '解析成功', 'hops': hops,
                    '最终URL': public_url(current), '曾HTTPS升级': upgraded,
                    '响应SHA256': hashlib.sha256(raw).hexdigest(),
                    '响应字节': len(raw), '单季记录数': len(rows)}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            return {'代码': code, '状态': '最终响应或解析失败', 'hops': hops,
                    '最终URL': public_url(current), '曾HTTPS升级': upgraded,
                    '错误': type(exc).__name__ + ': ' + str(exc)[:300]}
    return {'代码': code, '状态': '重定向过多', 'hops': hops}


def run(codes=DEFAULT_CODES, output=None, session=None):
    if session is None and os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('真实财经请求只允许 GitHub Actions')
    session = session or requests.Session()
    report = {'生成时间': dt.datetime.now(BJ).isoformat(),
              '来源分支': os.environ.get('GITHUB_REF_NAME', '离线测试'),
              '来源SHA': os.environ.get('GITHUB_SHA', '离线测试'),
              '来源运行': os.environ.get('GITHUB_RUN_ID', '离线测试'),
              '说明': '仅记录跳转协议、域名、路径、状态和解析摘要；不保存正文；TLS验证保持开启。',
              '结果': [trace(session, code) for code in codes]}
    if output:
        output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codes', nargs='+', default=list(DEFAULT_CODES))
    parser.add_argument('--output', type=Path, default=ROOT / 'logs/test/T13c/重定向诊断.json')
    args = parser.parse_args()
    report = run(tuple(args.codes), args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
