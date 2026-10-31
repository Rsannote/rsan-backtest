"""T14b独立读原始价格和T8最后修订，核对两代规则及固定公开窗口；全程离线。"""
import argparse
import collections
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import statistics
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from auto_judgment_common import atomic_text
from mainline_cases_data import HUMAN_FIELDS
from verify_t14_cases import WINDOWS

SUPERSEDED = 'v0 草案，已被 v1 取代'


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def rows(path):
    with Path(path).open(newline='') as stream:
        return list(csv.DictReader(stream))


def independent_conditions(frame, days, codes, candidates, version):
    """不用生产daily_conditions；独立验证同键最后空值、全27排名与分位阈值。"""
    frame = frame.drop_duplicates(['date', 'code'], keep='last')
    blocks = {}
    for key in ('相对沪深300_60', '占比_28', '占比一年分位_28'):
        values = pd.to_numeric(frame[key], errors='coerce')
        source = frame.assign(**{key: values.where(values.map(lambda x: math.isfinite(x) if pd.notna(x) else False))})
        blocks[key] = source.pivot(index='date', columns='code', values=key).reindex(index=days, columns=codes)
    relative = blocks['相对沪深300_60'][candidates]
    ranks = relative.rank(axis=1, ascending=False, method='first')
    ranks = ranks.where(relative.notna().sum(axis=1) == 27, axis=0)
    if version == 'v0':
        amount = blocks['占比_28']
        complete = amount.notna().sum(axis=1).eq(28) & amount.ge(0).all(axis=1) & amount.sum(axis=1).sub(1).abs().le(1e-7)
        money = amount[candidates].rank(axis=1, ascending=False, method='first').where(complete, axis=0)
        funding_ok = money.le(5).where(money.notna())
    else:
        money = blocks['占比一年分位_28'][candidates]
        money = money.where(money.ge(0) & money.le(100))
        funding_ok = money.ge(70).where(money.notna())
    states = {}
    for code in candidates:
        result = []
        for a, b in zip(ranks[code], funding_ok[code]):
            if (pd.notna(a) and a > 3) or (pd.notna(b) and not b):
                result.append(False)
            elif pd.notna(a) and pd.notna(b):
                result.append(True)
            else:
                result.append(None)
        states[code] = result
    return states, ranks, funding_ok


def grouped_hits(states, minimum=40):
    groups = []
    for position, value in enumerate(states):
        if value is not True:
            continue
        if not groups or position - groups[-1][-1] > 6:
            groups.append([])
        groups[-1].append(position)
    return [g for g in groups if len(g) >= minimum]


def terminal_status(states, hits):
    end = min(hits[-1] + 6, len(states) - 1)
    tail = states[hits[-1] + 1:end + 1]
    confirmed = len(tail) == 6 and all(v is False for v in tail)
    status = ('已确认结束' if confirmed else '数据中断待核实') if len(tail) == 6 else (
        '数据不足待确认' if any(v is None for v in tail) else '待中断确认')
    return status, confirmed, end


def stats(records):
    values = [finite(r['区间内相对沪深300超额']) for r in records]
    values = [v for v in values if v is not None]
    return {'段数': len(records), '超额有效段数': len(values),
            '平均区间超额': statistics.mean(values) if values else None,
            '超额为负段数': sum(v < 0 for v in values),
            '已确认结束段数': sum(r.get('状态') == '已确认结束' for r in records),
            '数据中断段数_计入统计': sum(r.get('状态') == '数据中断待核实' for r in records),
            '母集说明': '本版本全部案例（含数据中断、尚未确认），均值和负超额计数只用同一组有限端点超额；不按收益或状态筛样本'}


def verify(root=ROOT):
    root = Path(root).resolve()
    output = root / 'logs/test/T14b'
    config = json.loads((root / 'system/主线案例参数.json').read_text())
    assert [config[k] for k in ('识别版本', '相对强弱前几名', '占比一年分位下限', '最少实际命中日',
                                '最多桥接交易日', '采用段数下限', '采用段数上限')] == ['v1', 3, 70, 40, 5, 10, 60]
    report = json.loads((output / '建库报告.json').read_text())
    assert report['采用v1'] and 10 <= report['案例数'] <= 60
    registry = rows(root / 'system/行业清单.csv')
    names = {r['申万代码']: r['行业'] for r in registry}
    codes = sorted(set(names) - {'801960', '801970', '801980'})
    candidates = sorted(r['申万代码'] for r in registry if r['主线判定'] == '参与' and r['申万代码'] in codes)
    assert len(codes) == 28 and len(candidates) == 27
    benchmark = pd.read_csv(root / 'data/行情库/指数/sh000300.csv', dtype=str, keep_default_na=False)
    benchmark = benchmark.drop_duplicates('date', keep='last').set_index('date')
    benchmark = pd.to_numeric(benchmark.close) * pd.to_numeric(benchmark.factor)
    benchmark = benchmark.loc[(benchmark.index >= '2005-01-01') & (benchmark.index <= report['计算截止'])].sort_index()
    calendar = list(benchmark.index)
    days = [day for day in calendar if day >= report['数据起点']]
    day_index = {day: i for i, day in enumerate(calendar)}
    flow = pd.read_csv(root / 'data/资金轮动/日表.csv', dtype=str, keep_default_na=False, low_memory=False)
    flow = flow.drop_duplicates(['date', 'code'], keep='last')
    prices = {}
    for code in candidates:
        frame = pd.read_csv(root / 'data/资金轮动/原始/申万' / (code + '.csv'), dtype=str, keep_default_na=False)
        frame = frame.drop_duplicates('date', keep='last')
        prices[code] = {r['date']: finite(r['close']) for r in frame.to_dict('records')}
    all_rows = rows(root / 'data/事件库/主线.csv')
    latest = {r['事件id']: r for r in all_rows}
    current = {k: r for k, r in latest.items() if '-ML1-' in k and r['状态'] != '输入修订后不再满足'}
    proof = json.loads((output / 'v0原字节保护.json').read_text())
    csv_bytes = (root / 'data/事件库/主线.csv').read_bytes()
    original = csv_bytes[:proof['原字节']]
    assert hashlib.sha256(original).hexdigest() == proof['SHA256'], '旧CSV原字节已变化'
    frozen = {r['事件id']: r for r in csv.DictReader(io.StringIO(original.decode('utf-8-sig')))}
    assert len(proof['v0事件id']) == 18 and set(frozen) == set(proof['v0事件id'])
    retirements = collections.Counter(r['事件id'] for r in all_rows if r['事件id'] in frozen and r['状态'] == SUPERSEDED)
    assert set(retirements) == set(frozen) and all(n == 1 for n in retirements.values()), 'v0取代状态应每事件只追加一次'
    for event_id, old in frozen.items():
        assert latest[event_id]['状态'] == SUPERSEDED
        for key in old:
            if key not in HUMAN_FIELDS + ['状态', '程序说明', '追加时间', '更新原因']:
                assert latest[event_id][key] == old[key], (event_id, key, 'v0机器原值被重写')
    human = collections.defaultdict(dict)
    first = set()
    for row in all_rows:
        if '-ML1-' in row['事件id'] and row['事件id'] not in first:
            assert not any(row[k] for k in HUMAN_FIELDS), '程序新建v1时填写了人工列'
            first.add(row['事件id'])
        for key in HUMAN_FIELDS:
            if row.get(key):
                human[row['事件id']][key] = row[key]
    for event_id, values in human.items():
        assert all(latest[event_id][k] == v for k, v in values.items()), '未保留最近非空人工文字'
    states, ranks, money = independent_conditions(flow, days, codes, candidates, 'v1')
    expected = {}
    for code in candidates:
        for hits in grouped_hits(states[code]):
            expected[f'EVT-SW{code}-ML1-{days[hits[0]]}'] = (code, hits)
    assert set(current) == set(expected), 'v1结果与独立命中位置分组不符'
    checked = []
    for event_id, (code, hits) in expected.items():
        row = current[event_id];start, end = days[hits[0]], days[hits[-1]]
        assert (row['开始'], row['结束'], row['成立日期']) == (start, end, days[hits[39]])
        assert int(row['实际命中交易日']) == len(hits)
        assert int(row['桥接跨度交易日']) == hits[-1] - hits[0] + 1
        assert int(row['持续交易日']) == day_index[end] - day_index[start]
        internal = [states[code][i] for i in range(hits[0], hits[-1] + 1) if states[code][i] is not True]
        assert int(row['桥接已知失败日']) == sum(v is False for v in internal)
        assert int(row['桥接未知日']) == sum(v is None for v in internal)
        status, confirmed, tail_end = terminal_status(states[code], hits)
        assert row['状态'] == status and row['确认结束日期'] == (days[tail_end] if confirmed else '')
        a, b = prices[code].get(start), prices[code].get(end)
        if a is not None and b is not None and a > 0 and b > 0:
            amplitude = b / a - 1;excess = amplitude - (benchmark[end] / benchmark[start] - 1)
            assert abs(float(row['幅度']) - amplitude) < 1e-9
            assert abs(float(row['区间内相对沪深300超额']) - excess) < 1e-9
        else:
            assert not row['幅度'] and not row['区间内相对沪深300超额']
        complete = all(prices[c].get(d) is not None and prices[c][d] > 0 for c in candidates for d in (start, end))
        if complete:
            ordered = sorted(candidates, key=lambda c: (-(prices[c][end] / prices[c][start] - 1), c))
            assert row['同期第二强代码'] == ordered[1]
        else:
            assert not row['同期第二强代码']
        window = json.loads(row['启动前20日五个维度的读数'])
        assert [r['date'] for r in window['读数']] == calendar[max(0, day_index[start] - 20):day_index[start]]
        checked.append({'事件id': event_id, '实际命中': len(hits), '成立日期': row['成立日期'],
                        '状态': row['状态'], '区间超额': row['区间内相对沪深300超额']})
    legacy_states, _, _ = independent_conditions(flow, days, codes, candidates, 'v0')
    legacy = []
    for code in candidates:
        for hits in grouped_hits(legacy_states[code]):
            start, end = days[hits[0]], days[hits[-1]];a, b = prices[code].get(start), prices[code].get(end)
            excess = b / a - benchmark[end] / benchmark[start] if a and b else None
            legacy.append({'区间内相对沪深300超额': excess, '状态': terminal_status(legacy_states[code], hits)[0]})
    comparisons = []
    for group, start, end, targets in WINDOWS:
        for label, code in targets:
            item = {'公开组': group, '对象': label, '窗口开始': start, '窗口结束': end, '申万代码': code}
            if code is None:
                item.update({'窗口交集案例': [], '状态': '无一级行业同义映射',
                             '原因': '概念或风格横跨一级行业，不手工选代理强行匹配'})
            else:
                indices = [i for i, day in enumerate(days) if start <= day <= end]
                observed = [states[code][i] for i in indices]
                overlaps = [r for r in current.values() if r['申万代码'] == code and r['开始'] <= end
                            and r['结束'] >= start and r['成立日期'] <= end]
                longest = max((len(h) for h in grouped_hits(observed, 1)), default=0)
                item.update({'可用交易日': len(indices), '相对前3日': sum(pd.notna(ranks.at[days[i], code]) and ranks.at[days[i], code] <= 3 for i in indices),
                             '自身分位至少70日': sum(pd.notna(money.at[days[i], code]) and bool(money.at[days[i], code]) for i in indices),
                             '共同命中日': sum(v is True for v in observed), '未知日': sum(v is None for v in observed),
                             '最长连接段实际命中': longest,
                             '窗口交集案例': [r['事件id'] + '（成立' + r['成立日期'] + '）' for r in overlaps],
                             '状态': '存在已成立区间交集' if overlaps else 'v1未识别',
                             '原因': '交集不代表覆盖整轮；成立日期单列' if overlaps else f'固定前3和自身分位>=70的最长可连接段仅{longest}个实际命中；不跨超过5日的断点拼接'})
                for key in ('相对前3日', '自身分位至少70日'):
                    item[key] = int(item[key])
            comparisons.append(item)
    version_stats = {'v0已归档18段': stats(list(frozen.values())), 'v0同输入重算': stats(legacy), 'v1同输入重算': stats(list(current.values()))}
    public = json.loads((root / 'logs/test/T14/公开记录出处.json').read_text())
    result = {'计算截止': report['计算截止'], '数据起点': report['数据起点'], '事先采用范围': [10, 60], '采用v1': True,
              'v0原字节保护': proof, 'v0取代修订次数': dict(retirements), 'v0v1对照': version_stats, '独立区间核对': checked,
              '公开窗口对照': comparisons, '公开来源': public, '输入SHA256': report['输入SHA256'],
              '说明': '同输入v0/v1均使用同一最新T8截止、同一28口径和原始价格端点；另列归档v0防止混淆。当前成分/最后修订历史非严格时点，不测试交易策略。'}
    atomic_text(output / '验收与对照.json', json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    lines = ['# T14b v0/v1独立核验与公开窗口对照', '',
             f"同输入比较：{report['数据起点']}至{report['计算截止']}；固定相对前3、自身占比一年分位≥70、40命中、5桥接。v1独立核对{len(checked)}段，处于事先10至60段范围，采用v1；未调70。", '',
             '|版本|段数|超额有效段数|平均区间超额|超额为负段数|数据中断段（计入）|', '|---|---:|---:|---:|---:|---:|']
    for version, values in version_stats.items():
        mean = '缺' if values['平均区间超额'] is None else f"{values['平均区间超额']:.4%}"
        lines.append(f"|{version}|{values['段数']}|{values['超额有效段数']}|{mean}|{values['超额为负段数']}|{values['数据中断段数_计入统计']}|")
    lines += ['', '母集为每版本的全部案例，包含数据中断及未确认段，不按收益或确认状态筛选。均值与负超额段数都只用同一组有有限端点超额的案例，表中列有效样本数；本次全部有值。区间超额=行业价格收益减同期沪深300价格收益，对有值案例等权平均，不是组合净值或策略回测。归档v0保留原数值，不能将其附属五维按新T13数据重写。', '',
              '|公开组|对象|交易日|相对前3日|自身分位≥70日|共同命中|未知|最长段命中|窗口交集案例（成立日）|结果/差异|',
              '|---|---|---:|---:|---:|---:|---:|---:|---|---|']
    for item in comparisons:
        lines.append('|' + '|'.join([item['公开组'], item['对象'], *[str(item.get(k, '—')) for k in
                     ('可用交易日', '相对前3日', '自身分位至少70日', '共同命中日', '未知日', '最长连接段实际命中')],
                     '；'.join(item['窗口交集案例']) or '无', item['状态'] + '：' + item['原因']]) + '|')
    lines += ['', '## 旧字节、人工文字与来源', '',
              f"冻结v0旧CSV完整前缀{proof['原字节']}字节，SHA256 {proof['SHA256']}；18事件仅追加已取代状态，机器原值、起止日、旧参数及全部人工原文保留。v1首次记录的人工列全空。", '',
              '六组公开叙事沿用T14已定位的一手基金正式报告，全文定位与限制如下；本次没有重新联网取PDF。公开叙事不作为调整70的目标。', '', public['核验说明'], '']
    for group in dict.fromkeys(w[0] for w in WINDOWS):
        entry = public[group];lines += ['### ' + group, '', entry['结论'], '']
        for source in entry['来源']:
            lines.append(f"- [{source['标题']}]({source['URL']})，{source['页码']}。{source['核验']}。")
        lines.append('')
    lines += ['2015个别文档全文未复验、2019—2021材料分期支持、2024普通红利与央企红利口径不同，沿用原限制；互联网+/新能源/红利不硬映射成单一一级行业。', '']
    atomic_text(output / '验收与对照.md', '\n'.join(lines))
    print(json.dumps({'v0v1对照': version_stats, 'v1独立核对': len(checked), 'v0旧字节保持': True, '公开对象': len(comparisons)}, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    verify(parser.parse_args().root)
