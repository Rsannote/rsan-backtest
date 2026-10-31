#!/usr/bin/env python3
"""主线雷达 · 历史回放统计（T15）。只统计，不交易，不挑参数。

在 28 行业口径的全部历史上跑一遍阶段判定（scripts/mainline_stage.py），统计：
1. 每个阶段"进入"之后 5/10/20/60 个交易日，行业相对沪深300 的超额（中位数、均值、胜率、样本数），
   并给出全部行业日的无条件基线；
2. "确认"对照 T14 v1 历史主线案例（data/事件库/主线.csv 中 ML1）的覆盖率、提前/滞后天数和误报率；
3. 大盘门槛开 / 关两种情况的对比；
4. 每个参数上下各调一档时，关键数字怎么变（敏感性表）；
5. 业绩维度逐年覆盖率（潜伏阶段只能用业绩维度，热度历史缺失）；
6. 按上位类型分组的确认后表现。

超额 = 行业收盘价格收益 − 沪深300 收盘价格收益，从进入当天收盘起算（指标统计，不是可成交的策略收益）。
窗口可重叠，未做显著性检验。

用法：python3 scripts/mainline_replay.py [--quick] [--out logs]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mainline_stage as ms  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HORIZONS = [5, 10, 20, 60]
MILESTONES = ['潜伏', '启动', '确认', '拥挤', '退潮']


def forward_excess(inputs: dict) -> dict[int, pd.DataFrame]:
    closes = inputs['closes']
    hs = inputs['hs300']
    out = {}
    for n in HORIZONS:
        ind = closes.shift(-n) / closes - 1
        bench = hs.shift(-n) / hs - 1
        out[n] = ind.sub(bench, axis=0)
    return out


def milestone_rows(events: list[dict]) -> pd.DataFrame:
    rows = []
    for e in events:
        for mark in str(e.get('里程碑') or '').split('、'):
            if mark:
                rows.append({'date': e['date'], 'code': e['code'], '里程碑': mark, 'gate': e.get('gate', ''),
                             '上位类型': e.get('上位类型', '')})
    return pd.DataFrame(rows, columns=['date', 'code', '里程碑', 'gate', '上位类型'])


def summarize(values: pd.Series) -> dict:
    v = values.dropna()
    if v.empty:
        return {'样本': 0, '中位数': None, '均值': None, '胜率': None}
    return {'样本': int(len(v)), '中位数': float(v.median()), '均值': float(v.mean()), '胜率': float((v > 0).mean())}


def attach_forward(marks: pd.DataFrame, fwd: dict[int, pd.DataFrame]) -> pd.DataFrame:
    marks = marks.copy()
    for n, frame in fwd.items():
        stacked = frame.stack()
        marks[f'超额{n}'] = [stacked.get((d, c), np.nan) for d, c in zip(marks.date, marks.code)]
    return marks


def stage_table(marks: pd.DataFrame, fwd: dict[int, pd.DataFrame]) -> list[dict]:
    rows = []
    base = {n: summarize(fwd[n].stack()) for n in HORIZONS}
    rows.append({'里程碑': '基线（全部行业日）', **{f'{n}日': base[n] for n in HORIZONS}})
    for m in MILESTONES:
        sub = marks[marks['里程碑'] == m]
        rows.append({'里程碑': m, **{f'{n}日': summarize(sub[f'超额{n}']) for n in HORIZONS}})
    return rows


def load_cases(root: Path) -> pd.DataFrame:
    rows = list(csv.DictReader(open(root / 'data/事件库/主线.csv', encoding='utf-8')))
    latest = {}
    for r in rows:
        latest[r['事件id']] = r
    cases = [r for k, r in latest.items() if '-ML1-' in k and '取代' not in r.get('状态', '') and '失效' not in r.get('状态', '')]
    df = pd.DataFrame(cases)
    df['code'] = df['申万代码'].astype(str)
    return df[['事件id', 'code', '行业', '开始', '结束', '成立日期', '上位类型', '区间内相对沪深300超额']]


def case_alignment(stages: pd.DataFrame, marks: pd.DataFrame, cases: pd.DataFrame) -> dict:
    days = list(stages.index)
    pos = {d: i for i, d in enumerate(days)}
    covered, lags, detail = 0, [], []
    for c in cases.itertuples():
        if c.code not in stages.columns:
            continue
        window = stages.loc[c.开始:c.结束, c.code]
        hit = window[window.isin(['确认', '拥挤'])]
        confirm_marks = marks[(marks.code == c.code) & (marks['里程碑'] == '确认') &
                              (marks.date >= c.开始) & (marks.date <= c.结束)]
        ok = not confirm_marks.empty
        first = confirm_marks.date.min() if ok else None
        lag = None
        if ok and c.成立日期 in pos:
            lag = pos[first] - pos[c.成立日期]
            lags.append(lag)
        covered += ok
        detail.append({'事件id': c.事件id, '行业': c.行业, '开始': c.开始, '结束': c.结束, '案例成立': c.成立日期,
                       '首次确认': first, '确认相对案例成立(交易日)': lag, '区间内确认或拥挤天数': int(len(hit))})
    confirms = marks[marks['里程碑'] == '确认']
    inside = 0
    for m in confirms.itertuples():
        sub = cases[(cases.code == m.code) & (cases.开始 <= m.date) & (cases.结束 >= m.date)]
        inside += not sub.empty
    total = len(confirms)
    return {'案例数': int(len(cases)), '被确认覆盖': int(covered),
            '覆盖率': covered / len(cases) if len(cases) else None,
            '确认相对案例成立中位数': float(np.median(lags)) if lags else None,
            '确认次数': int(total), '落在案例内': int(inside),
            '误报率': (total - inside) / total if total else None, '明细': detail}


def case_forward(cases: pd.DataFrame, fwd: dict[int, pd.DataFrame]) -> dict:
    """历史主线案例在成立日（第40个命中日，当时可知）之后的超额：检验"确认后跟随"本身有没有用。"""
    rows = []
    for c in cases.itertuples():
        row = {'上位类型': c.上位类型}
        for n in HORIZONS:
            frame = fwd[n]
            row[n] = frame[c.code].get(c.成立日期, np.nan) if c.code in frame else np.nan
        rows.append(row)
    df = pd.DataFrame(rows)
    out = {'全部': {f'{n}日': summarize(df[n]) for n in HORIZONS}}
    for t, g in df.groupby('上位类型'):
        out[t or '未补写'] = {f'{n}日': summarize(g[n]) for n in HORIZONS}
    return out


def momentum_reference(inputs: dict, fwd: dict[int, pd.DataFrame]) -> list[dict]:
    """参照：只按过去 N 日相对沪深300 强弱排名，最强 3 个和最弱 3 个行业之后的超额（描述统计）。"""
    closes, hs = inputs['closes'], inputs['hs300']
    rows = []
    for look in (20, 60, 120, 250):
        past = (closes / closes.shift(look) - 1).sub(hs / hs.shift(look) - 1, axis=0)
        rank = past.rank(axis=1, ascending=False)
        count = past.notna().sum(axis=1)
        for n in (20, 60):
            top = fwd[n].where(rank <= 3).stack().dropna()
            bottom = fwd[n].where(rank.ge(count - 2, axis=0)).stack().dropna()
            rows.append({'回看': look, '之后': n, '最强3': summarize(top), '最弱3': summarize(bottom)})
    return rows


def earnings_coverage(inputs: dict) -> dict:
    earn = inputs['data']['业绩_利润加速占比']
    years = earn.index.str[:4]
    return {y: float(v) for y, v in earn.notna().groupby(years).mean().mean(axis=1).items()}


def key_numbers(inputs, params, fwd, cases, gate=True) -> dict:
    stages, events = ms.run_machine(inputs, params, gate=gate)
    marks = attach_forward(milestone_rows(events), fwd)
    align = case_alignment(stages, marks, cases)
    conf = marks[marks['里程碑'] == '确认']
    retreat = marks[marks['里程碑'] == '退潮']
    start = marks[marks['里程碑'] == '启动']
    return {'启动次数': int(len(start)), '确认次数': int(len(conf)),
            '确认后20日超额中位数': summarize(conf['超额20'])['中位数'],
            '确认后20日胜率': summarize(conf['超额20'])['胜率'],
            '退潮后20日超额中位数': summarize(retreat['超额20'])['中位数'],
            '覆盖率': align['覆盖率'], '误报率': align['误报率']}, stages, events, marks, align


def fmt(x, pct=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return '—'
    return f'{x * 100:.2f}%' if pct else f'{x:g}'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--quick', action='store_true', help='跳过敏感性表')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    out = args.out or root / 'logs'
    out.mkdir(parents=True, exist_ok=True)
    raw = json.loads((root / 'system/主线参数.json').read_text(encoding='utf-8'))
    params = ms.load_params(root)
    inputs = ms.build_inputs(root, 28)
    fwd = forward_excess(inputs)
    cases = load_cases(root)

    base, stages, events, marks, align = key_numbers(inputs, params, fwd, cases, gate=True)
    nogate, *_ = key_numbers(inputs, params, fwd, cases, gate=False)
    table = stage_table(marks, fwd)
    by_type = {t: summarize(g['超额20']) for t, g in marks[marks['里程碑'] == '启动'].groupby('上位类型')}
    case_type = {}
    conf = marks[marks['里程碑'] == '确认']
    by_gate = {g or '无数据': summarize(sub['超额20']) for g, sub in conf.groupby('gate')}
    after_cases = case_forward(cases, fwd)
    momentum = momentum_reference(inputs, fwd)
    sensitivity = []
    if not args.quick:
        for name, spec in raw['参数'].items():
            step = spec['步长']
            for direction in (-1, 1):
                value = round(spec['值'] + direction * step, 4)
                if isinstance(spec['值'], int) and not isinstance(step, float):
                    value = int(value)
                if value <= 0:
                    continue
                trial = ms.load_params(root, {name: value})
                numbers, *_ = key_numbers(inputs, trial, fwd, cases)
                sensitivity.append({'参数': name, '草案值': spec['值'], '试验值': value, **numbers})
                print(f'敏感性 {name}={value}: {numbers}', flush=True)

    now = dt.datetime.now(ms.BJ).isoformat()
    report = {'生成时间': now, '参数版本': params['_版本'], '口径': 28,
              '区间': [inputs['days'][0], inputs['days'][-1]], '阶段表': table, '关键数字': base,
              '关闭大盘门槛': nogate, '确认按大盘状态': by_gate, '启动按上位类型': by_type,
              '案例对照': align, '案例成立后': after_cases, '强弱参照': momentum,
              '业绩覆盖率': earnings_coverage(inputs), '敏感性': sensitivity}
    (out / '主线回放.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=ms._json) + '\n',
                                      encoding='utf-8')

    L = ['# 主线雷达 · 历史回放（T15）', '',
         f'生成：{now}（北京时间）；参数：{params["_版本"]}；口径：28 行业（27 个候选）；区间 {inputs["days"][0]}—{inputs["days"][-1]}。',
         '', '只统计、不交易。超额=行业收盘价格收益−沪深300收益，从进入当天收盘起算；窗口可重叠，未做显著性检验。'
         '热度维度历史缺失，"潜伏"只用业绩维度；大盘攻守 2016 年起才有，2014–2015 年不加门槛。', '',
         '## 一、各阶段进入之后的超额', '', '|里程碑|' + '|'.join(f'{n}日中位数|{n}日胜率' for n in HORIZONS) + '|样本(20日)|',
         '|---|' + '---:|---:|' * len(HORIZONS) + '---:|']
    for row in table:
        cells = []
        for n in HORIZONS:
            s = row[f'{n}日']
            cells += [fmt(s['中位数']), fmt(s['胜率'])]
        L.append(f"|{row['里程碑']}|" + '|'.join(cells) + f"|{row['20日']['样本']}|")
    L += ['', '## 二、确认 vs 历史主线案例（T14 v1，42 段）', '',
          f"- 被确认覆盖：{align['被确认覆盖']}/{align['案例数']}（{fmt(align['覆盖率'])}）",
          f"- 首次确认相对案例成立日：中位数 {fmt(align['确认相对案例成立中位数'], False)} 个交易日（负数=更早）",
          f"- 确认共 {align['确认次数']} 次，落在案例区间内 {align['落在案例内']} 次，误报率 {fmt(align['误报率'])}",
          '', '|案例|行业|区间|案例成立|首次确认|相对天数|区间内确认/拥挤天数|', '|---|---|---|---|---|---:|---:|']
    for d in align['明细']:
        L.append(f"|{d['事件id'][4:]}|{d['行业']}|{d['开始']}—{d['结束']}|{d['案例成立']}|{d['首次确认'] or '未确认'}|"
                 f"{'' if d['确认相对案例成立(交易日)'] is None else d['确认相对案例成立(交易日)']}|{d['区间内确认或拥挤天数']}|")
    L += ['', '### 历史主线案例"成立"之后的超额', '',
          '案例成立日 = 第 40 个命中日，当时就能知道。这一节检验"主线确认后再跟进"本身有没有用，与阶段规则无关。', '',
          '|分组|' + '|'.join(f'{n}日中位数|{n}日胜率' for n in HORIZONS) + '|样本|', '|---|' + '---:|---:|' * len(HORIZONS) + '---:|']
    for label, s in after_cases.items():
        L.append(f'|{label}|' + '|'.join(f"{fmt(s[f'{n}日']['中位数'])}|{fmt(s[f'{n}日']['胜率'])}" for n in HORIZONS)
                 + f"|{s['20日']['样本']}|")
    L += ['', '注意：案例的上位类型是 Claude 事后补写的，补写时已经知道后来的走势，按类型分组的结果可能有事后偏差，只能当线索，不能当证据。']
    L += ['', '### 参照：只看过去强弱排名', '', '|回看|之后|最强3中位数|最强3胜率|最弱3中位数|最弱3胜率|', '|---:|---:|---:|---:|---:|---:|']
    for r in momentum:
        L.append(f"|{r['回看']}日|{r['之后']}日|{fmt(r['最强3']['中位数'])}|{fmt(r['最强3']['胜率'])}|"
                 f"{fmt(r['最弱3']['中位数'])}|{fmt(r['最弱3']['胜率'])}|")
    L += ['', '## 三、大盘门槛', '', '|情况|启动|确认|确认后20日中位数|确认后20日胜率|退潮后20日中位数|覆盖率|误报率|',
          '|---|---:|---:|---:|---:|---:|---:|---:|']
    for label, k in [('门槛开（草案）', base), ('门槛关', nogate)]:
        L.append(f"|{label}|{k['启动次数']}|{k['确认次数']}|{fmt(k['确认后20日超额中位数'])}|{fmt(k['确认后20日胜率'])}|"
                 f"{fmt(k['退潮后20日超额中位数'])}|{fmt(k['覆盖率'])}|{fmt(k['误报率'])}|")
    L += ['', '确认当天的大盘状态：', '']
    for g, s in by_gate.items():
        L.append(f"- {g}：样本 {s['样本']}，20日超额中位数 {fmt(s['中位数'])}，胜率 {fmt(s['胜率'])}")
    L += ['', '## 四、启动时的上位类型', '', '历史上新闻和催化日历缺失，"验证事件型"基本判不出来，只能看环境切换型和未明。', '']
    for t, s in by_type.items():
        L.append(f"- {t or '未标'}：样本 {s['样本']}，20日超额中位数 {fmt(s['中位数'])}，胜率 {fmt(s['胜率'])}")
    L += ['', '## 五、业绩维度逐年覆盖率（27 个候选的有值比例）', '', '|年|覆盖率|', '|---|---:|']
    for y, v in report['业绩覆盖率'].items():
        L.append(f'|{y}|{fmt(v)}|')
    if sensitivity:
        L += ['', '## 六、敏感性（每个参数上下各一档，其余不变）', '',
              '|参数|草案|试验|启动|确认|确认后20日中位数|确认后20日胜率|退潮后20日中位数|覆盖率|误报率|',
              '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
              f"|（草案）|—|—|{base['启动次数']}|{base['确认次数']}|{fmt(base['确认后20日超额中位数'])}|{fmt(base['确认后20日胜率'])}|"
              f"{fmt(base['退潮后20日超额中位数'])}|{fmt(base['覆盖率'])}|{fmt(base['误报率'])}|"]
        for s in sensitivity:
            L.append(f"|{s['参数']}|{s['草案值']}|{s['试验值']}|{s['启动次数']}|{s['确认次数']}|{fmt(s['确认后20日超额中位数'])}|"
                     f"{fmt(s['确认后20日胜率'])}|{fmt(s['退潮后20日超额中位数'])}|{fmt(s['覆盖率'])}|{fmt(s['误报率'])}|")
    L += ['', '回放只给 Claude 和 Rsan 定参数用，程序不挑最优参数。不构成投资建议。', '']
    (out / '主线回放.md').write_text('\n'.join(L), encoding='utf-8')
    print(f'输出：{out / "主线回放.md"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
