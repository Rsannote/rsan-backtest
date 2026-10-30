#!/usr/bin/env python3
"""T14历史主线案例：只读T8/T9核心输入，以独立草案参数离线回补。"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re

import pandas as pd

from auto_judgment_common import BJ, atomic_text, read_csv
from mainline_cases_data import (NEW, FIELDS, SignalWindow, append_records,
                                 dump, latest_frame, latest_records, numeric, write_lock, CaseData as Data)
from mainline_cases_data import price_table

ROOT = Path(__file__).resolve().parents[1]
VERSION = '主线案例草案v0'


def parameters(path):
    result = json.loads(Path(path).read_text())
    for key in ('相对强弱前几名', '最少实际命中日', '最多桥接交易日'):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == '最多桥接交易日' else 1):
            raise ValueError('无效案例草案参数：' + key)
    share = result['成交占比前比例']
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not math.isfinite(share) or not 0 < share <= 1:
        raise ValueError('成交占比前比例须在(0,1]')
    return result


def registry(root):
    rows = read_csv(Path(root) / 'system/行业清单.csv')
    names = {r['申万代码']: r for r in rows}
    if len(rows) != len(names) or len(names) != 31:
        raise ValueError('Claude行业清单必须唯一且完整31行业')
    codes = sorted(set(names) - NEW)
    candidates = [c for c in codes if names[c]['主线判定'] == '参与']
    if len(codes) != 28 or len(candidates) != 27 or names['801230']['主线判定'] != '排除':
        raise ValueError('固定28行业/27候选与综合排除约定不符，需审核后修改格式')
    return names, codes, candidates


def daily_conditions(flow, days, codes, candidates, config):
    """全候选排名才可参与判定；一个已知失败足以判False，其余缺数为None。"""
    # 调用者提供去重后的T8，不使用T8包含综合的旧rank，不计算新的收益或平滑。
    codes, candidates = sorted(codes), sorted(candidates)
    selected = flow[flow.code.isin(codes) & flow.date.isin(days)].copy()
    if selected.duplicated(['date', 'code']).any():
        raise ValueError('T8必须先取同键最后修订')
    matrices = {}
    for field in ('相对沪深300_60', '占比_28', '收盘'):
        selected[field] = numeric(selected[field])
        matrices[field] = selected.pivot(index='date', columns='code', values=field).reindex(index=days, columns=codes)
    relative = matrices['相对沪深300_60'][candidates]
    shares = matrices['占比_28']
    # 分母含综合；缺任何一行/负占比/占比和不为1时不冒充完整28行业。
    share_complete = shares.notna().all(axis=1) & shares.ge(0).all(axis=1) & shares.sum(axis=1).sub(1).abs().le(1e-7)
    relative_complete = relative.notna().all(axis=1)
    relative_rank = relative.rank(axis=1, ascending=False, method='first').where(relative_complete, axis=0)
    share_rank = shares[candidates].rank(axis=1, ascending=False, method='first').where(share_complete, axis=0)
    states = {}
    for code in candidates:
        values = []
        for day in days:
            a, b = relative_rank.at[day, code], share_rank.at[day, code]
            rel_ok = None if pd.isna(a) else bool(a <= config['相对强弱前几名'])
            share_ok = None if pd.isna(b) else bool(b / len(candidates) <= config['成交占比前比例'])
            values.append(False if rel_ok is False or share_ok is False else
                          True if rel_ok is True and share_ok is True else None)
        states[code] = values
    audit = [{'date': day, '相对完整': bool(relative_complete.at[day]), '占比完整': bool(share_complete.at[day]),
              '相对有效候选': int(relative.loc[day].notna().sum()),
              '占比有效行业': int(shares.loc[day].notna().sum()),
              '命中行业数': sum(states[c][i] is True for c in candidates),
              '未知行业数': sum(states[c][i] is None for c in candidates)} for i, day in enumerate(days)]
    return states, matrices, audit


def episodes(states, minimum=40, bridge=5):
    """实际命中日成立，最多5个非命中日桥接；未知不能证明结束。"""
    if minimum < 1 or bridge < 0 or any(v is not True and v is not False and v is not None for v in states):
        raise ValueError('命中序列或案例参数无效')
    result = []
    hits = []
    gap = []
    internal = []

    def finish(cutoff, confirmed=None, status='待中断确认'):
        if len(hits) >= minimum:
            result.append({'start': hits[0], 'end': hits[-1], 'formed': hits[minimum - 1],
                           'confirmed': confirmed, 'cutoff': cutoff, 'status': status,
                           'hits': len(hits), 'span': hits[-1] - hits[0] + 1,
                           'bridge_false': sum(states[i] is False for i in internal),
                           'bridge_unknown': sum(states[i] is None for i in internal),
                           'tail_false': sum(states[i] is False for i in gap),
                           'tail_unknown': sum(states[i] is None for i in gap)})
    for i, state in enumerate(states):
        if state is True:
            if hits:
                internal.extend(gap)
            hits.append(i)
            gap = []
        elif hits:
            gap.append(i)
            if len(gap) > bridge:
                known = all(states[j] is False for j in gap)
                finish(i, i if known else None, '已确认结束' if known else '数据中断待核实')
                hits, gap, internal = [], [], []
    if hits:
        finish(len(states) - 1, status='数据不足待确认' if any(states[i] is None for i in gap) else '待中断确认')
    return result


def interval_ranking(prices, days, start, end, candidates, benchmark):
    """27候选两端价格完整才给整体排名；包括本行业，取整体第2。"""
    window = [days[start], days[end]]
    block = prices.reindex(index=window, columns=candidates)
    complete = block.notna().all(axis=0) & block.gt(0).all(axis=0)
    missing = [c for c in candidates if not complete[c]]
    if missing:
        return [], '27候选区间端点行情不完整，第二强缺；缺代码=' + ';'.join(missing)
    base = benchmark[window[-1]] / benchmark[window[0]] - 1
    scores = [(c, float(block[c].iloc[-1] / block[c].iloc[0] - 1 - base)) for c in candidates]
    return sorted(scores, key=lambda x: (-x[1], x[0])), '27候选含本行业；两端完整的区间价格收益减沪深300价格收益降序第2；同值代码升序'


def make_record(data, signals, names, code, event, days, prices, candidates, config):
    start, end = days[event['start']], days[event['end']]
    global_start, global_end = data.index[start], data.index[end]
    row = {k: '' for k in FIELDS}
    row.update(data.features(global_start))
    notes = [row.get('程序说明', ''), f"事后案例开始日不能当时交易；成立日期为第{config['最少实际命中日']}个实际命中日；参数草案未调优"]
    benchmark = dict(zip(data.days, data.values))
    block = prices.loc[start:end, code]
    if all(pd.notna(v) and v > 0 for v in (block.iloc[0], block.iloc[-1])):
        amplitude = float(block.iloc[-1] / block.iloc[0] - 1)
        excess = amplitude - (benchmark[end] / benchmark[start] - 1)
    else:
        amplitude = excess = None
        notes.append('本行业区间端点行情不完整，幅度与超额缺')
    evidence = json.loads(row['特征来源'])
    evidence['行业价格'] = {'path': f'data/资金轮动/原始/申万/{code}.csv', '列': 'close', '版本': '同日最后修订',
                           '上游': '申万官网价格指数；不含分红', '开始': start, '结束': end}
    # 公共涨幅字段针对事件对象（本行业），不是误用T9沪深300自身涨幅。
    for n in (60, 120):
        dates = data.days[global_start - n:global_start + 1] if global_start >= n else []
        values = prices.reindex(dates)[code] if dates else pd.Series(dtype=float)
        value = float(values.iloc[-1] / values.iloc[0] - 1) if len(values) == n + 1 and values.notna().all() and values.gt(0).all() else None
        row[f'启动前{n}日涨幅'] = value
        if value is None:
            notes.append(f'本行业启动前{n}日涨幅缺数据')
    ranked, ranking_note = interval_ranking(prices, days, event['start'], event['end'], candidates, benchmark)
    second = ranked[1] if len(ranked) >= 2 else None
    market, market_notes = signals.row(start, code)
    evidence['五维窗口'] = {'path': 'data/主线/历史信号表_28.csv', '范围': 'T-20..T-1', '时点': '各观测日23:59:59'}
    row.update({'事件id': f'EVT-SW{code}-ML-{start}', '类型': '主线', '对象': names[code]['行业'],
                '行业': names[code]['行业'], '申万代码': code, '开始': start, '结束': end,
                '幅度': amplitude, '区间内相对沪深300超额': excess, '持续交易日': global_end - global_start,
                '实际命中交易日': event['hits'], '桥接跨度交易日': event['span'],
                '桥接已知失败日': event['bridge_false'], '桥接未知日': event['bridge_unknown'],
                '尾部已知失败日': event['tail_false'], '尾部未知日': event['tail_unknown'],
                '成立日期': days[event['formed']], '确认结束日期': days[event['confirmed']] if event['confirmed'] is not None else '',
                '状态': event['status'], '计算截止': days[event['cutoff']],
                '启动前20日五个维度的读数': json.dumps(signals.window(global_start, code), ensure_ascii=False, sort_keys=True),
                '启动时大盘攻守': market.get('环境_大盘攻守') if market else None,
                '同期第二强的行业': names[second[0]]['行业'] if second else None,
                '同期第二强代码': second[0] if second else None, '同期第二强相对超额': second[1] if second else None,
                '同期第二强说明': ranking_note, '特征来源': json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                '程序说明': '；'.join(x for x in notes + market_notes if x), '程序版本': VERSION,
                '识别来源': 'T8最后修订：相对沪深300_60、占比_28；27候选排名；28分母含综合；不要求正收益',
                '参数': json.dumps(config, ensure_ascii=False, sort_keys=True)})
    return row


def total_table(records, cutoff, config):
    rows = sorted((r for r in records.values() if r['状态'] != '输入修订后不再满足'), key=lambda r: (r['开始'], r['申万代码']))
    spec = f"{config['相对强弱前几名']} / {config['成交占比前比例']:.0%} / {config['最少实际命中日']}实际命中 / {config['最多桥接交易日']}桥接"
    lines = ['# 历史主线案例总表', '', f'计算截止：{cutoff}。草案固定{spec}；尚未作为交易信号或正式参数。', '',
             '幅度为行业价格指数收益，超额为收益差；持续日是端点索引差，跨度含开始日。开始是事后回溯日期，须到成立日才满40个命中。', '',
             '|行业|事件id|开始|结束（最后命中）|幅度|超额|持续日|实际命中|跨度|成立日期|确认结束|状态|同期整体第2行业|',
             '|---|---|---|---|---:|---:|---:|---:|---:|---|---|---|---|']
    def pct(v):
        return '缺' if v in ('', None) else f'{float(v):.4%}'
    for r in rows:
        lines.append('|' + '|'.join([r['行业'], r['事件id'], r['开始'], r['结束'], pct(r['幅度']),
                                    pct(r['区间内相对沪深300超额']), str(r['持续交易日']), str(r['实际命中交易日']),
                                    str(r['桥接跨度交易日']), r['成立日期'], r['确认结束日期'], r['状态'],
                                    r['同期第二强的行业'] or '缺']) + '|')
    invalid = [r['事件id'] for r in records.values() if r['状态'] == '输入修订后不再满足']
    lines += ['', f'现行案例{len(rows)}段；输入修订后失效{len(invalid)}段（CSV历史保留）。', '',
              '五维逐日原值及缺失覆盖见CSV，启动前窗口不含开始日；公共宏观按T9开始日18:00与T6可见时刻。',
              '人工触发事件、上位类型、支撑、退潮原因等由Claude判读，程序留空并保护既有非空。', '']
    return '\n'.join(lines)


def published_cutoff(root):
    """全局高水位同时查报告与全部总表快照；不能只查较早结束的事件。"""
    cutoffs = []
    report_path = Path(root) / 'logs/test/T14/建库报告.json'
    if report_path.exists():
        report = json.loads(report_path.read_text())
        cutoffs.append(report['计算截止'])
    table_path = Path(root) / 'data/事件库/主线_总表.md'
    if table_path.exists():
        text = table_path.read_bytes().decode('utf-8')
        dates = re.findall(r'^计算截止：(\d{4}-\d{2}-\d{2})[。\s]', text, flags=re.MULTILINE)
        if text.strip() and not dates:
            raise ValueError('已有主线总表无法识别全局计算截止，拒绝覆盖或截短历史')
        cutoffs.extend(dates)
    for day in cutoffs:
        dt.date.fromisoformat(day)
    return max(cutoffs, default=None)


def _run_locked(root, asof=None, now=None):
    root = Path(root).resolve()
    now = (now or dt.datetime.now(BJ)).astimezone(BJ)
    config_path = root / 'system/主线案例参数.json'
    config = parameters(config_path)
    names, codes, candidates = registry(root)
    flow_path = root / 'data/资金轮动/日表.csv'
    # 最后修订在日期筛选/代码筛选/聚合之前读取；最新空值绝不回退旧行。
    flow = latest_frame(flow_path)
    unknown = set(flow.code) - set(names)
    if unknown:
        raise ValueError('T8含Claude清单以外代码：' + ';'.join(sorted(unknown)))
    if flow.empty:
        raise ValueError('T8无数据')
    data = Data(root, now)
    cutoff = min(asof or data.days[-1], data.days[-1], flow.date.max())
    dt.date.fromisoformat(cutoff)
    previous_cutoff = published_cutoff(root)
    if previous_cutoff and previous_cutoff > cutoff:
        raise ValueError('已发布全局计算截止晚于本次，拒绝截短历史；请用隔离--root回放')
    days = [d for d in data.days if flow.date.min() <= d <= cutoff]
    if not days:
        raise ValueError('T8与沪深300无可用交集')
    states, _, audit = daily_conditions(flow, days, codes, candidates, config)
    prices, price_paths = price_table(root, codes, [d for d in data.days if d <= cutoff])
    signals = SignalWindow(root, data.days)
    records = []
    for code in candidates:
        for event in episodes(states[code], config['最少实际命中日'], config['最多桥接交易日']):
            records.append(make_record(data, signals, names, code, event, days, prices, candidates, config))
    records.sort(key=lambda r: (r['开始'], r['申万代码']))
    target = root / 'data/事件库/主线.csv'
    # --asof写入root为隔离副本；若直接回退生产截止会失效未来记录，明确拒绝。
    previous = latest_records(target)
    if previous and any(r['计算截止'] > cutoff for r in previous.values() if r['状态'] != '输入修订后不再满足'):
        raise ValueError('已有案例计算截止晚于本次，拒绝截短历史；请用隔离--root回放')
    added = append_records(target, records, now)
    latest = latest_records(target)
    # Markdown总表也是data历史，只追加不同内容的快照，不改写旧字节。
    table_path = root / 'data/事件库/主线_总表.md'
    table = total_table(latest, cutoff, config)
    old_table = table_path.read_bytes().decode('utf-8') if table_path.exists() else ''
    if not old_table or old_table.split('\n<!-- T14快照 -->\n')[-1] != table:
        atomic_text(table_path, old_table + ('\n<!-- T14快照 -->\n' if old_table else '') + table)
    manifest_paths = [flow_path, config_path, root / 'system/行业清单.csv'] + signals.paths + price_paths
    manifest = dict(data.paths)
    manifest.update({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in manifest_paths if p.exists()})
    report = {'title': 'T14历史主线案例', '生成时间': now.isoformat(), '计算截止': cutoff, '数据起点': days[0],
              '案例数': len(records), '追加行数': added, '状态计数': dict(collections.Counter(r['状态'] for r in records)),
              '观察行业': 28, '候选行业': 27, '参数': config, '输入SHA256': manifest,
              '缺失日期': [r for r in audit if not r['相对完整'] or not r['占比完整']],
              '已知不成立与未知': '一个完整排名条件失败即不命中；不能判定任一失败且缺完整排名为未知；缺不是0',
              '限制': ['T8/T13最后修订，历史非逐日时点版本', '五维不参与识别，业绩缺不阻止建库',
                       '公开叙事的概念/风格未必对应一级行业，不能靠调参强行吻合', '不生成判断、不回测、不执行T15阶段'],
              '现行事件id': [r['事件id'] for r in records]}
    output = root / 'logs/test/T14'
    dump(output / '建库报告.json', report)
    dump(output / '日条件审计.json', audit)
    print(json.dumps({k: report[k] for k in ('计算截止', '数据起点', '案例数', '追加行数', '状态计数')}, ensure_ascii=False))
    return report


def run(root=ROOT, asof=None, now=None):
    root = Path(root).resolve()
    # 同根目录的完整建库互斥，截止检查、CSV、总表和报告基于同一串行版本。
    with write_lock(root / '.t14-run-identity'):
        return _run_locked(root, asof, now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--asof', help='隔离副本的历史计算截止；不伪称逐日原始版本')
    parser.add_argument('--backfill', action='store_true', help='全历史重算，结果只追加，同输入幂等')
    args = parser.parse_args()
    run(args.root, args.asof)


if __name__ == '__main__':
    main()
