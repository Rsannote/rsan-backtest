"""T14独立离线验收与固定公开案例窗口对照；不抓行情，不调阈值。"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mainline_cases as cases
from mainline_cases_data import dump, latest_frame, latest_records, price_table
from auto_judgment_common import atomic_text

# 来自开发计划的预先给定窗口；概念/风格不可为了吻合而硬映射一级行业。
WINDOWS = [
    ('2015上半年', '2015-01-01', '2015-06-30', [('计算机', '801750'), ('互联网+', None)]),
    ('2017全年', '2017-01-01', '2017-12-31', [('食品饮料', '801120'), ('家用电器', '801110')]),
    ('2019至2021', '2019-01-01', '2021-12-31', [('电力设备', '801730'), ('新能源', None)]),
    ('2023上半年', '2023-01-01', '2023-06-30', [('传媒', '801760'), ('计算机', '801750')]),
    ('2024全年', '2024-01-01', '2024-12-31', [('银行', '801780'), ('红利', None)]),
    ('2026上半年', '2026-01-01', '2026-06-30', [('电子', '801080'), ('通信', '801770')]),
]


def verify(root=ROOT):
    root = Path(root).resolve()
    config = cases.parameters(root / 'system/主线案例参数.json')
    if cases.rule_version(config) == 'v1':
        from verify_t14b_cases import verify as verify_v1
        return verify_v1(root)
    assert [config[k] for k in ('相对强弱前几名', '成交占比前比例', '最少实际命中日', '最多桥接交易日')] == [3, .2, 40, 5], '验收固定草案，不自动校准'
    names, codes, candidates = cases.registry(root)
    report = json.loads((root / 'logs/test/T14/建库报告.json').read_text())
    data = cases.Data(root, dt.datetime.fromisoformat(report['生成时间']))
    flow = latest_frame(root / 'data/资金轮动/日表.csv')
    days = [d for d in data.days if report['数据起点'] <= d <= report['计算截止']]
    states, matrices, audit = cases.daily_conditions(flow, days, codes, candidates, config)
    prices, _ = price_table(root, codes, data.days)
    latest = latest_records(root / 'data/事件库/主线.csv')
    current = {k: r for k, r in latest.items() if r['状态'] != '输入修订后不再满足'}
    expected = {}
    # 使用直观扫描交叉核对程序划段，而非只验证CSV自己的一致性。
    for code in candidates:
        hit_positions = [i for i, state in enumerate(states[code]) if state is True]
        groups = []
        for position in hit_positions:
            if not groups or position - groups[-1][-1] > 6:
                groups.append([])
            groups[-1].append(position)
        for hits in groups:
            if len(hits) >= 40:
                expected[f'EVT-SW{code}-ML-{days[hits[0]]}'] = (code, hits)
    assert set(current) == set(expected), '识别区间与独立命中位置分组不符'
    checked = []
    for event_id, (code, hits) in expected.items():
        row = current[event_id]
        start, end = days[hits[0]], days[hits[-1]]
        assert row['开始'] == start and row['结束'] == end
        assert row['成立日期'] == days[hits[39]]
        assert int(row['实际命中交易日']) == len(hits)
        assert int(row['持续交易日']) == data.index[end] - data.index[start]
        assert int(row['桥接跨度交易日']) == hits[-1] - hits[0] + 1
        assert int(row['桥接跨度交易日']) == int(row['实际命中交易日']) + int(row['桥接已知失败日']) + int(row['桥接未知日'])
        if row['确认结束日期']:
            i = days.index(row['确认结束日期'])
            assert i == hits[-1] + 6 and all(states[code][j] is False for j in range(hits[-1] + 1, i + 1))
        a, b = prices.at[start, code], prices.at[end, code]
        if row['幅度']:
            amplitude = b / a - 1
            assert abs(float(row['幅度']) - amplitude) < 1e-9
            excess = amplitude - (data.values[data.index[end]] / data.values[data.index[start]] - 1)
            assert abs(float(row['区间内相对沪深300超额']) - excess) < 1e-9
        # 第二强独立从两端排序，不排除本行业，不以已识别案例替代全27候选。
        endpoint = prices.loc[[start, end], candidates]
        if endpoint.notna().all().all() and endpoint.gt(0).all().all():
            ordered = sorted(candidates, key=lambda c: (-float(endpoint[c].iloc[-1] / endpoint[c].iloc[0] - 1), c))
            assert row['同期第二强代码'] == ordered[1]
        else:
            assert not row['同期第二强代码']
        window = json.loads(row['启动前20日五个维度的读数'])
        assert [r['date'] for r in window['读数']] == data.days[max(0, data.index[start] - 20):data.index[start]]
        checked.append({'事件id': event_id, '实际命中': len(hits), '成立日期': row['成立日期'],
                        '状态': row['状态'], '幅度': row['幅度'], '超额': row['区间内相对沪深300超额']})
    comparisons = []
    for group, start, end, targets in WINDOWS:
        for label, code in targets:
            item = {'公开组': group, '窗口开始': start, '窗口结束': end, '对象': label, '申万代码': code}
            if code is None:
                item.update({'状态': '无一级行业同义映射', '原因': '概念或风格横跨多个一级行业，不能手工选代理强行匹配', '窗口交集案例': []})
                comparisons.append(item)
                continue
            selected = [i for i, day in enumerate(days) if start <= day <= end]
            observed = [states[code][i] for i in selected]
            runs = cases.episodes(observed, minimum=1, bridge=5)
            rel = matrices['相对沪深300_60'][candidates]
            ranks = rel.rank(axis=1, method='first', ascending=False).where(rel.notna().all(axis=1), axis=0)
            share = matrices['占比_28']
            share_ok = share.notna().all(axis=1) & share.ge(0).all(axis=1) & share.sum(axis=1).sub(1).abs().le(1e-7)
            share_ranks = share[candidates].rank(axis=1, method='first', ascending=False).where(share_ok, axis=0)
            overlap = [r for r in current.values() if r['申万代码'] == code and r['开始'] <= end and r['结束'] >= start]
            known_overlap = [r for r in overlap if r['成立日期'] <= end]
            ids = [r['事件id'] + '（成立' + r['成立日期'] + '）' for r in overlap]
            maximum = max((r['hits'] for r in runs), default=0)
            relative_days = int(sum(ranks.at[days[i], code] <= 3 for i in selected))
            share_days = int(sum(share_ranks.at[days[i], code] / 27 <= .2 for i in selected))
            if share_days < 40:
                reason = f'成交占比前5仅{share_days}日，相对前3有{relative_days}日；双条件实际命中不足40'
            elif relative_days < 40:
                reason = f'相对前3仅{relative_days}日，成交占比前5有{share_days}日；双条件实际命中不足40'
            else:
                reason = f'前3与占比前5同时命中最长可连接段仅{maximum}日，未达40；窗口总命中不能跨超过5日断点拼接'
            item.update({'可用交易日': len(selected), '60日全排名前3日': relative_days,
                         '占比前5日': share_days,
                         '共同命中日': sum(v is True for v in observed), '已知不成立日': sum(v is False for v in observed),
                         '未知日': sum(v is None for v in observed), '窗口内最长连接段实际命中日': maximum,
                         '窗口交集案例': ids, '窗口结束前已成立交集段': len(known_overlap),
                         '状态': '存在区间交集' if known_overlap else '草案未识别',
                         '原因': '至少一段与给定窗口相交，成立日另列；不代表整个窗口每天均为主线' if known_overlap else
                                 reason})
            if group == '2019至2021':
                annual = {}
                for year in ('2019', '2020', '2021'):
                    values = [states[code][i] for i in selected if days[i].startswith(year)]
                    annual[year] = {'命中日': sum(v is True for v in values), '未知日': sum(v is None for v in values),
                                    '最长连接段命中日': max((r['hits'] for r in cases.episodes(values, 1, 5)), default=0)}
                item['逐年统计'] = annual
            comparisons.append(item)
    # numpy标量须转为普通int才能严格JSON输出。
    for item in comparisons:
        for key in ('60日全排名前3日', '占比前5日'):
            if key in item:
                item[key] = int(item[key])
    public = json.loads((root / 'logs/test/T14/公开记录出处.json').read_text())
    result = {'独立区间核对': checked, '案例对照': comparisons, '公开来源': public,
              '结论': '固定草案如实对照；公开叙事不能证明日度规则。未调参数，未写归因。',
              '输入SHA256': report['输入SHA256']}
    dump(root / 'logs/test/T14/验收与对照.json', result)
    lines = ['# T14固定草案验收与公开案例对照', '',
             f"数据：{report['数据起点']}至{report['计算截止']}；固定3 / .20 / 40实际命中 / 5桥接。独立命中位置分组核对{len(checked)}段通过。", '',
             '对照窗口在开发计划中预先给定，窗口交集不等于连续覆盖整轮行情；成立日期防止把事后开始日误当当时信号。', '',
             '|组|对象|交易日|前3日|占比前5日|共同命中|未知|最长段实际命中|窗口交集案例（成立日）|结果/差异原因|',
             '|---|---|---:|---:|---:|---:|---:|---:|---|---|']
    for item in comparisons:
        lines.append('|' + '|'.join([item['公开组'], item['对象'], *[str(item.get(k, '—')) for k in
                     ('可用交易日', '60日全排名前3日', '占比前5日', '共同命中日', '未知日', '窗口内最长连接段实际命中日')],
                     '；'.join(item['窗口交集案例']) or '无', item['状态'] + '：' + item['原因']]) + '|')
    lines += ['', '## 2019—2021分年统计', '']
    for item in comparisons:
        if '逐年统计' in item:
            for year, counts in item['逐年统计'].items():
                lines.append(f"- {year}电力设备：命中{counts['命中日']}日，未知{counts['未知日']}日，最长段实际命中{counts['最长连接段命中日']}日。")
    lines += ['', '## 一手正式发布记录与口径限制', '', public['核验说明'], '']
    for group in dict.fromkeys(r[0] for r in WINDOWS):
        entry = public[group]
        lines += ['### ' + group, '', entry['结论'], '']
        for source in entry['来源']:
            lines.append(f"- [{source['标题']}]({source['URL']})，发布{source['发布日期']}，{source['页码']}。{source['核验']}。")
        lines.append('')
    lines += ['## 验收边界', '',
              '- 公开文档支持叙事与样本口径；日度规则命中仅由已存T8计算，不把叙事当参数拟合目标。',
              '- 2015计算机的一个文档全文未复验；其余已核验正式文本，不能笼统称六组每个细节均完成独立验证。',
              '- 缺数据不是已知不成立；未知不凑40也不单独确认退潮，最多5日仅作桥接。',
              '- 二级/概念/风格与一级行业不强映射；2019–2021分期支持，2024普通红利全年不能冒称跑赢300。', '']
    atomic_text(root / 'logs/test/T14/验收与对照.md', '\n'.join(lines))
    print(f'独立核对{len(checked)}段；六组公开窗口{len(comparisons)}个对象；固定参数，未调优')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    verify(parser.parse_args().root)
