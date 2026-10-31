#!/usr/bin/env python3
"""T13c 独立验收：27 行业完整性、上市日账本、2013 起财报及 3×3 覆盖手算。"""
import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from earnings_coverage import read_listings, read_members
from earnings_history import deadline, load_v3, visibility


def rows(path):
    if not path.exists(): return []
    with path.open(encoding='utf-8', newline='') as stream: return list(csv.DictReader(stream))


def latest_side(root):
    values = {}
    for row in rows(root / 'data/财报/覆盖率明细.csv'):
        values[(row['date'], row['code'])] = row
    return values


def quarters(root, code):
    records = {}
    announcements = {}
    for row in rows(root / 'data/财报/单季' / (code + '.csv')):
        records[row['报告期']] = row
        if row.get('公告日'): announcements[row['报告期']] = row['公告日']
    for row in rows(root / 'data/财报/全市场单季' / (code + '.csv')):
        item = dict(row)
        if row['报告期'] in announcements: item['公告日'] = announcements[row['报告期']]
        records[row['报告期']] = item
    return list(records.values())


def manual(root, member, period, day, listings, q_signal, cfg):
    boundary = dt.date.fromisoformat(visibility(period)[:10]); asof = dt.date.fromisoformat(day)
    codes = sorted({str(r['证券代码']).zfill(6) for r in member['rows']})
    eligible = [c for c in codes if c in listings and dt.date.fromisoformat(listings[c]['上市日期']) < boundary]
    unknown = [c for c in codes if c not in listings]
    not_yet = [c for c in codes if c not in unknown and c not in eligible]
    valid = passed = 0
    for code in eligible:
        available = []
        for row in quarters(root, code):
            if row.get('公告日') and row['公告日'] >= day: continue
            if row.get('可见时刻') and dt.datetime.fromisoformat(row['可见时刻']).date() > asof: continue
            available.append(row)
        sig = q_signal(available, asof)
        if not sig or sig['q'] != period or any(not math.isfinite(sig[k]) for k in ('g', 'gprev', 'grev')): continue
        valid += 1
        passed += int(cfg['g_min'] < sig['g'] <= cfg['g_max'] and sig['acc'] > 0 and sig['gprev'] > cfg['prev_min'] and sig['grev'] > 0)
    return {'当前成分总量': len(codes), '已上市分母': len(eligible), '未上市家数': len(not_yet),
            '上市日期未知家数': len(unknown), '可评估家数': valid, '加速家数': passed,
            '季报覆盖率': valid / len(eligible) if eligible else None,
            '行业利润加速占比': passed / valid if len(eligible) and valid >= 10 and valid * 5 >= len(eligible) * 3 else None}


def same(a, b):
    if a in ('', None) and b is None: return True
    try: return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    except (TypeError, ValueError): return str(a) == str(b)


def verify(root=ROOT, require_complete=False):
    root = Path(root); members = read_members(root); listings = read_listings(root)
    if len(members) != 27: raise AssertionError('正式成分快照不是27候选行业')
    universe = sorted({str(r['证券代码']).zfill(6) for m in members.values() for r in m['rows']})
    if len(universe) != 4996: raise AssertionError('正式27行业独立股票数不是探测锁定的4996')
    ledger = [json.loads(line) for line in (root / 'data/财报/上市日期.jsonl').read_text().splitlines() if line]
    observed = {row['代码'] for row in ledger}
    missing_listing = sorted(set(universe) - observed)
    state_path = root / 'data/财报/全市场单季/采集状态.jsonl'
    states = [json.loads(line) for line in state_path.read_text().splitlines() if line] if state_path.exists() else []
    observed_finance = {p.stem for p in (root / 'data/财报/全市场单季').glob('*.csv')} | {r['代码'] for r in states if r.get('状态') in ('成功', '无2013起历史')}
    missing_finance = sorted(set(universe) - observed_finance)
    if require_complete and (missing_listing or missing_finance):
        raise AssertionError(f'正式上市日/财报覆盖不完整：{len(missing_listing)}/{len(missing_finance)}')
    for path in (root / 'data/财报/全市场单季').glob('*.csv'):
        if path.name == '采集状态.jsonl': continue
        for row in rows(path):
            if row['报告期'] < '2013-01-01' or row['法定截止'] != deadline(row['报告期']).isoformat() or row['可见时刻'] != visibility(row['报告期']):
                raise AssertionError('2013起单季或法定可见时点不一致：' + str(path))
    side = latest_side(root)
    if require_complete and not side: raise AssertionError('覆盖率明细尚未生成')
    num, q_signal, cfg = load_v3(root)
    checks = []
    for code in ('801030', '801730', '801890'):
        candidates = [r for (day, sector), r in sorted(side.items()) if sector == code and r.get('季报期')]
        if not candidates: continue
        positions = sorted({0, len(candidates)//2, len(candidates)-1})
        for pos in positions:
            row = candidates[pos]; got = manual(root, members[code], row['季报期'], row['date'], listings, q_signal, cfg)
            for key, value in got.items():
                if not same(row.get(key), value): raise AssertionError(f'{code} {row["date"]} {key} 手算={value} 程序={row.get(key)}')
            checks.append((code, row, got))
    if require_complete and len(checks) != 9: raise AssertionError('手算核对不足3行业×3日期')
    names = {code: members[code]['rows'][0].get('行业', '') for code in members}
    lines = ['# T13c 覆盖率手算核对', '', '分母只含 IPO 严格早于季度规则可见日的当前成分；上市日未知排除并单列。当前成分快照回溯仍有幸存者偏差。', '',
             '|行业代码|日期|季度|当前总量|已上市分母|未上市|上市日未知|有效|加速|覆盖率|加速占比|',
             '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for code, row, got in checks:
        def show(v): return '缺' if v is None else f'{v:.6f}' if isinstance(v, float) else str(v)
        lines.append('|' + '|'.join([code, row['date'], row['季报期']] + [show(got[k]) for k in ('当前成分总量','已上市分母','未上市家数','上市日期未知家数','可评估家数','加速家数','季报覆盖率','行业利润加速占比')]) + '|')
    out = root / 'logs/test/T13c/覆盖率手算核对.md'; out.parent.mkdir(parents=True, exist_ok=True); out.write_text('\n'.join(lines) + '\n')
    result = {'候选行业': len(members), '独立股票': len(universe), '上市日成功': len(listings),
              '上市日未观测': len(missing_listing), '财报未覆盖': len(missing_finance), '手算行': len(checks),
              '当前成分快照幸存者偏差': True, '状态': '通过' if not missing_listing and not missing_finance and len(checks) == 9 else '未完成'}
    (root / 'logs/test/T13c/正式验收.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False)); return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--root', type=Path, default=ROOT); parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args(); verify(args.root, args.require_complete)
