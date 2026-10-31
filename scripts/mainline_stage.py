#!/usr/bin/env python3
"""主线雷达 · 阶段判定（T15）。

按 system/主线规则.md 第四、五节和 system/主线参数.json，给每个申万一级行业每天判一个阶段：
潜伏 / 启动 / 确认 / 拥挤 / 退潮 / 无，并在进入"启动"时打上位类型标签。

只读已有产出，不联网、不重算上游指标：
- 主线信号表：scripts/mainline_signal_store.read_signal_table（基表 + 年度修订，同键取最后一条）
- 行业收盘：data/资金轮动/日表.csv（同 date+code 取最后一条）
- 沪深300：data/行情库/指数/sh000300.csv
- 大盘攻守：logs/市场状态_规则检验_逐日.csv（2016 起回补）+ data/市场状态/<月>/<日>.json（实时，优先）
- 新闻与催化：data/新闻/、system/催化日历.csv（只用于上位类型；2026-10-21 前没有新闻）

程序只给"规则阶段"这个指标，不下买卖结论。

用法：
    python3 scripts/mainline_stage.py            # 生成当前（31 口径）阶段与 28 口径历史阶段
    python3 scripts/mainline_stage.py --scope 28 --out <目录>   # 只算某个口径，输出到指定目录（回放用）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mainline_signal_store as store  # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))
STAGES = ['无', '潜伏', '启动', '确认', '拥挤', '退潮']


# ---------------------------------------------------------------- 参数

def load_params(root: Path, overrides: dict | None = None) -> dict:
    raw = json.loads((root / 'system/主线参数.json').read_text(encoding='utf-8'))
    params = {k: v['值'] for k, v in raw['参数'].items()}
    params.update(raw.get('上位类型', {}))
    if overrides:
        unknown = set(overrides) - set(params)
        if unknown:
            raise KeyError(f'未知参数：{sorted(unknown)}')
        params.update(overrides)
    params['_版本'] = raw.get('版本', '')
    return params


# ---------------------------------------------------------------- 输入

def industry_list(root: Path) -> pd.DataFrame:
    rows = list(csv.DictReader(open(root / 'system/行业清单.csv', encoding='utf-8')))
    df = pd.DataFrame(rows)
    df['code'] = df['申万代码'].astype(str)
    return df.set_index('code')


def read_closes(root: Path, codes: list[str]) -> pd.DataFrame:
    usecols = ['date', 'code', '收盘']
    df = pd.read_csv(root / 'data/资金轮动/日表.csv', usecols=usecols, dtype={'code': str, 'date': str})
    df = df[df.code.isin(codes)].drop_duplicates(['date', 'code'], keep='last')
    return df.pivot(index='date', columns='code', values='收盘').sort_index()


def read_hs300(root: Path) -> pd.Series:
    df = pd.read_csv(root / 'data/行情库/指数/sh000300.csv', dtype={'date': str})
    return df.set_index('date')['close'].astype(float)


def read_market_state(root: Path) -> pd.Series:
    """大盘攻守：实时存档优先，其余用回补检验表。2016 年以前没有。"""
    out: dict[str, str] = {}
    backfill = root / 'logs/市场状态_规则检验_逐日.csv'
    if backfill.exists():
        for row in csv.DictReader(open(backfill, encoding='utf-8')):
            if row.get('结论'):
                out[row['date']] = row['结论']
    for path in sorted((root / 'data/市场状态').glob('20*/*.json')):
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        day = obj.get('数据日期') or path.stem
        state = (obj.get('市场') or {}).get('结论')
        if state:
            out[str(day)] = state
    return pd.Series(out, dtype=object).sort_index()


def read_catalysts(root: Path) -> pd.DataFrame:
    path = root / 'system/催化日历.csv'
    if not path.exists():
        return pd.DataFrame(columns=['date', '相关行业'])
    rows = []
    for row in csv.DictReader(open(path, encoding='utf-8')):
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', row.get('日期', '')):
            rows.append({'date': row['日期'], '相关行业': row.get('相关行业', '')})
    return pd.DataFrame(rows, columns=['date', '相关行业'])


def pivot(signals: pd.DataFrame, column: str) -> pd.DataFrame:
    return signals.pivot(index='date', columns='code', values=column).sort_index()


def build_inputs(root: Path, scope: int) -> dict:
    signals = store.read_signal_table(root, scope)
    if signals.empty:
        raise SystemExit(f'信号表 {scope} 口径为空')
    signals = signals[signals['主线判定'] == '参与'].copy()
    codes = sorted(signals.code.unique())
    numeric = ['资金_占比5均较20均', '资金_占比60日分位', '资金_相对沪深300_20', '资金_占比5日变化',
               '拥挤_占比一年分位', '拥挤_偏离60日线', '业绩_利润加速占比', '环境_中国10年20日变化bp']
    for column in numeric:
        signals[column] = pd.to_numeric(signals[column], errors='coerce')
    data = {name: pivot(signals, name) for name in numeric}
    days = data['资金_相对沪深300_20'].index
    closes = read_closes(root, codes).reindex(days)
    hs300 = read_hs300(root).reindex(days)
    market = read_market_state(root).reindex(days)
    meta = industry_list(root)
    news_major = signals.pivot(index='date', columns='code', values='热度_重要度3当日条数') if '热度_重要度3当日条数' in signals else None
    return {'scope': scope, 'codes': codes, 'days': days, 'data': data, 'closes': closes, 'hs300': hs300,
            'market': market, 'meta': meta, 'news_major': news_major, 'catalysts': read_catalysts(root)}


# ---------------------------------------------------------------- 衍生特征

def derive(inputs: dict, params: dict) -> dict:
    d = inputs['data']
    rel20 = d['资金_相对沪深300_20']
    # 在每天有值的候选里排名（1=最强）；候选全缺的日子为 NaN
    rank20 = rel20.rank(axis=1, ascending=False, method='first')
    closes = inputs['closes']
    ma20 = closes.rolling(20, min_periods=18).mean()
    ind_ret = closes.pct_change(fill_method=None)
    hs_ret = inputs['hs300'].pct_change(fill_method=None)
    # 抗跌：近60日沪深300跌超1%的日子里，行业跌得更少的比例
    down = hs_ret <= -0.01
    down_mask = pd.DataFrame(np.repeat(down.to_numpy()[:, None], len(closes.columns), axis=1),
                             index=closes.index, columns=closes.columns)
    better = ind_ret.gt(hs_ret, axis=0) & down_mask
    down_count = down.astype(float).rolling(60, min_periods=1).sum()
    better_count = better.astype(float).rolling(60, min_periods=1).sum()
    resist = better_count.div(down_count.replace(0, np.nan), axis=0)
    # 业绩先行：利润加速占比 20 个交易日变化，在有效行业里排名
    earn = d['业绩_利润加速占比']
    earn_change = earn - earn.shift(20)
    earn_rank = earn_change.where(earn_change > 0).rank(axis=1, ascending=False, method='first')
    earn_valid = earn_change.notna().sum(axis=1)
    return {'rank20': rank20, 'valid20': rel20.notna().sum(axis=1), 'ratio': d['资金_占比5均较20均'],
            'pct60': d['资金_占比60日分位'], 'pct1y': d['拥挤_占比一年分位'], 'dev60': d['拥挤_偏离60日线'],
            'share_chg5': d['资金_占比5日变化'], 'close': closes, 'ma20': ma20, 'resist': resist,
            'down_count': down_count, 'earn_rank': earn_rank, 'earn_valid': earn_valid,
            'rel20': rel20, 'cn10': d['环境_中国10年20日变化bp']}


# ---------------------------------------------------------------- 状态机

def _v(frame: pd.DataFrame, i: int, code: str) -> float:
    value = frame[code].iat[i]
    return float(value) if pd.notna(value) else math.nan


def run_machine(inputs: dict, params: dict, gate: bool = True) -> tuple[pd.DataFrame, list[dict]]:
    """逐行业逐日推进。返回阶段表（date×code）和进入事件列表。"""
    f = derive(inputs, params)
    days = list(inputs['days'])
    market = inputs['market']
    stages = pd.DataFrame('无', index=inputs['days'], columns=inputs['codes'])
    events: list[dict] = []
    p = params
    for code in inputs['codes']:
        state = '无'
        start_streak = 0          # 启动条件连续满足天数
        start_since = None        # 本轮启动开始位置
        out_top = 0               # 启动期间掉出确认排名的天数
        fail_streak = 0           # 启动后排名连续跌出失败排名的天数
        confirmed = False         # 本轮是否已确认
        retreat_streak = 0
        rank_out_streak = 0
        retreat_since = None
        crowded_once = False      # 本轮是否已记过拥挤里程碑
        for i, day in enumerate(days):
            marks: list[str] = []
            rank = _v(f['rank20'], i, code)
            ratio = _v(f['ratio'], i, code)
            pct60 = _v(f['pct60'], i, code)
            pct1y = _v(f['pct1y'], i, code)
            dev60 = _v(f['dev60'], i, code)
            if math.isnan(rank):
                stages.iat[i, stages.columns.get_loc(code)] = state
                continue
            start_cond = (not math.isnan(ratio)) and ratio >= p['启动_占比提升'] and rank <= p['启动_强弱排名']
            start_streak = start_streak + 1 if start_cond else 0
            crowd_cond = (not math.isnan(pct1y) and pct1y >= p['拥挤_占比一年分位']) or \
                         (not math.isnan(dev60) and dev60 >= p['拥挤_偏离60日线'])
            new_state = state

            if state in ('无', '潜伏', '退潮'):
                if start_streak >= p['启动_持续天数']:
                    new_state = '启动'
                    start_since = i - int(p['启动_持续天数']) + 1
                    # 启动期从 5 个连续满足日的第一天算起；那几天里排名在前 N 之外的也计入"掉出"
                    out_top = int((f['rank20'][code].iloc[start_since:i] > p['确认_强弱排名']).sum())
                    fail_streak, confirmed, crowded_once = 0, False, False
                    marks.append('启动')
                elif state == '退潮':
                    if i - retreat_since >= p['退潮_持续天数']:
                        new_state = '无'
                else:
                    earn_rank = _v(f['earn_rank'], i, code)
                    earn_valid = f['earn_valid'].iat[i]
                    lead = (not math.isnan(earn_rank)) and earn_valid > 0 and \
                        earn_rank <= max(1, math.floor(earn_valid * p['潜伏_先行改善前比例']))
                    latent = (not math.isnan(pct1y)) and pct1y < p['潜伏_拥挤分位上限'] and lead and \
                        rank > p['潜伏_强弱排名下限']
                    new_state = '潜伏' if latent else '无'
            else:  # 启动 / 确认 / 拥挤
                if rank > p['确认_强弱排名']:
                    out_top += 1
                if not confirmed:
                    fail_streak = fail_streak + 1 if rank > p['启动_失败排名'] else 0
                    if fail_streak >= 5:
                        new_state = '无'
                        start_since = None
                    else:
                        defensive = gate and market.iat[i] == '守'
                        need_days = p['确认_启动天数'] + (p['大盘守_确认天数加'] if defensive else 0)
                        need_pct = p['确认_占比60日分位'] + (p['大盘守_分位加'] if defensive else 0)
                        resist = _v(f['resist'], i, code)
                        enough_down = f['down_count'].iat[i] >= p['确认_抗跌最少样本']
                        resist_ok = (not enough_down) or (not math.isnan(resist) and resist >= p['确认_抗跌比例'])
                        if (i - start_since + 1) >= need_days and out_top <= p['确认_允许掉出天数'] and \
                                not math.isnan(pct60) and pct60 >= need_pct and resist_ok:
                            confirmed = True
                            new_state = '确认'
                            marks.append('确认')
                        else:
                            new_state = '拥挤' if crowd_cond else '启动'
                if confirmed:
                    close, ma20 = _v(f['close'], i, code), _v(f['ma20'], i, code)
                    chg5 = _v(f['share_chg5'], i, code)
                    weak = (not math.isnan(close)) and (not math.isnan(ma20)) and close < ma20 and \
                        (not math.isnan(chg5)) and chg5 < 0
                    retreat_streak = retreat_streak + 1 if weak else 0
                    rank_out_streak = rank_out_streak + 1 if rank > p['退潮_排名跌出'] else 0
                    if retreat_streak >= p['退潮_连续天数'] or rank_out_streak >= p['退潮_排名跌出连续天数']:
                        new_state = '退潮'
                        retreat_since = i
                        marks.append('退潮')
                        confirmed, retreat_streak, rank_out_streak, start_since = False, 0, 0, None
                    else:
                        new_state = '拥挤' if crowd_cond else '确认'
            if new_state == '拥挤' and not crowded_once:
                crowded_once = True
                marks.append('拥挤')
            if new_state == '潜伏' and state != '潜伏':
                marks.append('潜伏')
            if new_state != state or marks:
                event = {'date': day, 'code': code, 'from': state, 'to': new_state,
                         'gate': market.iat[i] if isinstance(market.iat[i], str) else '',
                         '里程碑': '、'.join(marks)}
                if '启动' in marks:
                    event['上位类型'] = tag_type(inputs, f, i, code, p)
                events.append(event)
            state = new_state
            stages.iat[i, stages.columns.get_loc(code)] = state
    return stages, events


def tag_type(inputs: dict, f: dict, i: int, code: str, p: dict) -> str:
    """进入启动时的上位类型。"""
    meta = inputs['meta']
    duration = meta.at[code, '久期'] if code in meta.index else ''
    lookback = int(p.get('环境_回看天数', 20))
    window = f['cn10'][code].iloc[max(0, i - lookback):i]
    env = False
    if len(window.dropna()) and duration in ('长', '短'):
        move = window.dropna().iloc[-1]
        favour = (duration == '长' and move < 0) or (duration == '短' and move > 0)
        peers = [c for c in inputs['codes'] if c != code and c in meta.index and meta.at[c, '久期'] == duration]
        peer_rel = f['rel20'].iloc[i][peers].mean() if peers else math.nan
        env = favour and not math.isnan(peer_rel) and peer_rel > 0
    event = False
    days = inputs['days']
    lo = days[max(0, i - int(p.get('验证_回看天数', 10)))]
    hi = days[i]
    news = inputs['news_major']
    if news is not None and code in news:
        counts = pd.to_numeric(news[code].reindex(days).loc[lo:hi], errors='coerce')
        event = bool((counts > 0).any())
    name = meta.at[code, '行业'] if code in meta.index else ''
    cat = inputs['catalysts']
    if not cat.empty and not event:
        hit = cat[(cat.date >= lo) & (cat.date <= hi)]
        event = any(name and name in str(x) for x in hit['相关行业'])
    if env and event:
        return '共振'
    if env:
        return '环境切换型'
    if event:
        return '验证事件型'
    return '未明'


# ---------------------------------------------------------------- 输出

def write_outputs(root: Path, out: Path, scope: int, stages: pd.DataFrame, events: list[dict], inputs: dict,
                  params: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    meta = inputs['meta']
    long = stages.stack().rename('阶段').reset_index()
    long.columns = ['date', 'code', '阶段']
    long['行业'] = long.code.map(lambda c: meta.at[c, '行业'] if c in meta.index else '')
    appended = append_changed(out / f'阶段日表_{scope}.csv', long, params['_版本'])
    # 事件表是阶段日表的派生摘要，可整表刷新
    pd.DataFrame(events).to_csv(out / f'阶段事件_{scope}.csv', index=False)
    print(f'{scope}口径阶段日表追加 {appended} 行', flush=True)
    if scope != 31:
        return
    last = stages.index[-1]
    recent = sorted([e for e in events if e['date'] >= stages.index[max(0, len(stages) - 5)]], key=lambda e: (e['date'], e['code']))
    f = derive(inputs, params)
    rows = []
    for code in stages.columns:
        rows.append({'code': code, '行业': meta.at[code, '行业'] if code in meta.index else '',
                     '阶段': stages.at[last, code],
                     '20日强弱排名': None if pd.isna(f['rank20'].at[last, code]) else int(f['rank20'].at[last, code]),
                     '占比5日均较20日均': f['ratio'].at[last, code], '占比60日分位': f['pct60'].at[last, code],
                     '占比一年分位': f['pct1y'].at[last, code], '偏离60日线': f['dev60'].at[last, code]})
    now = dt.datetime.now(BJ).isoformat()
    report = {'数据日期': last, '生成时间': now, '参数版本': params['_版本'], '口径': scope,
              '行业': rows, '近5日阶段变化': recent}
    status = {'title': '主线阶段', 'data_date': last, 'generated_at': now, 'status': '正常', 'errors': [],
              '口径': scope, '参数版本': params['_版本']}
    (root / 'logs').mkdir(exist_ok=True)
    (root / 'logs/主线阶段_最新.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (out / '阶段_最新.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json) + '\n',
                                       encoding='utf-8')
    order = ['确认', '拥挤', '启动', '退潮', '潜伏', '无']
    lines = ['# 主线雷达 · 阶段', '', f'数据日期：{last}；生成：{now}（北京时间）；参数：{params["_版本"]}。'
             '程序按规则判定的阶段，是指标，不是买卖建议。', '']
    for stage in order:
        names = [r['行业'] for r in rows if r['阶段'] == stage]
        lines.append(f'- **{stage}**（{len(names)}）：{"、".join(names) if names else "无"}')
    lines += ['', '## 近 5 个交易日阶段变化', '']
    if recent:
        lines += ['|日期|行业|变化|上位类型|大盘|', '|---|---|---|---|---|']
        for e in recent:
            name = meta.at[e['code'], '行业'] if e['code'] in meta.index else e['code']
            lines.append(f"|{e['date']}|{name}|{e['from']}→{e['to']}|{e.get('上位类型', '')}|{e['gate']}|")
    else:
        lines.append('无')
    lines += ['', '催化分量、和历史哪次最像、要盯的验证点：【待判读】', '']
    (out / '阶段_最新.md').write_text('\n'.join(lines), encoding='utf-8')


def append_changed(path: Path, long: pd.DataFrame, version: str) -> int:
    """派生表写入规则（AGENTS.md 第 2 条）：按 date+code 与已存最后一条比较，只追加新增或阶段变化的行。"""
    now = dt.datetime.now(BJ).isoformat()
    long = long[['date', 'code', '行业', '阶段']].copy()
    long['参数版本'] = version
    if path.exists():
        old = pd.read_csv(path, dtype={'date': str, 'code': str}).drop_duplicates(['date', 'code'], keep='last')
        merged = long.merge(old[['date', 'code', '阶段', '参数版本']], on=['date', 'code'], how='left',
                            suffixes=('', '_旧'))
        changed = merged[(merged['阶段'] != merged['阶段_旧']) | (merged['参数版本'] != merged['参数版本_旧'])]
        rows = changed[['date', 'code', '行业', '阶段', '参数版本']].copy()
    else:
        rows = long
    if rows.empty:
        return 0
    rows['记录时间'] = now
    rows.to_csv(path, mode='a', header=not path.exists(), index=False)
    return int(len(rows))


def read_stage_table(path: Path) -> pd.DataFrame:
    """读阶段日表：同 date+code 取最后一条。"""
    return pd.read_csv(path, dtype={'date': str, 'code': str}).drop_duplicates(['date', 'code'], keep='last')


def _json(value):
    if isinstance(value, (np.floating, float)):
        return None if math.isnan(float(value)) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--scope', type=int, choices=[28, 31])
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    out = args.out or root / 'data/主线'
    params = load_params(root)
    for scope in ([args.scope] if args.scope else [28, 31]):
        inputs = build_inputs(root, scope)
        stages, events = run_machine(inputs, params)
        write_outputs(root, out, scope, stages, events, inputs, params)
        counts = pd.Series([e['to'] for e in events]).value_counts().to_dict()
        print(f'{scope}口径：{stages.index[0]}—{stages.index[-1]}，阶段进入次数 {counts}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
