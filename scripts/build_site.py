#!/usr/bin/env python3
"""轻量看板数据生成（T16）。只读取已有产出、不重算，写 site/data/*.json 给 site/index.html 读。

栏目：大盘、主线雷达（含提醒）、资金轮动、历史对照、宏观、判断命中率、系统状态。
每个栏目带"数据日期"和"是否过期"（取健康检查对应项的状态）。

用法：python3 scripts/build_site.py [--out site/data]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BJ = dt.timezone(dt.timedelta(hours=8))


def clean(value):
    """JSON 友好：NaN/inf → None，numpy → 原生类型。"""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, 'item') and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def health(root: Path) -> dict:
    report = read_json(root / 'logs/健康检查_最新.json') or {'items': []}
    return {i['id']: {'名称': i['name'], '状态': i['status'], '原因': (i.get('reasons') or [''])[0][:160]}
            for i in report['items']} | {'_checked_at': report.get('checked_at')}


def stale(h: dict, *ids: str) -> bool:
    return any(h.get(i, {}).get('状态') in ('延迟', '失败') for i in ids)


# ------------------------------------------------------------------ 栏目

def market(root: Path, h: dict) -> dict:
    state = read_json(root / 'data/市场状态/最新.json') or {}
    m = state.get('市场', {})
    temp = pd.read_csv(root / 'data/社区情绪/温度计.csv', encoding='utf-8-sig').tail(60)
    daily = pd.read_csv(root / 'data/市场状态/日指标.csv').tail(250)
    turnover = daily.iloc[:, 1].astype(float)
    pct = float((turnover <= turnover.iloc[-1]).mean() * 100)
    margin = pd.read_csv(root / 'data/市场状态/融资余额.csv').tail(6)
    m5 = float(margin.iloc[-1, 1] / margin.iloc[0, 1] - 1)
    return {'数据日期': state.get('数据日期'), '过期': stale(h, 'market_state', 'sentiment_index'),
            '结论': m.get('结论'), '冷热': m.get('冷热'), '总分': m.get('总分'), '依据': m.get('依据', []),
            '冷信号': m.get('冷信号', []), '热信号': m.get('热信号', []),
            '情绪温度': float(temp['温度'].iloc[-1]), '情绪日期': str(temp['date'].iloc[-1]),
            '情绪走势': {'date': temp['date'].astype(str).tolist(), '温度': temp['温度'].round(1).tolist()},
            '成交额亿': float(turnover.iloc[-1]), '成交额一年分位': round(pct, 1),
            '融资余额亿': float(margin.iloc[-1, 1]), '融资5日变化': m5, '融资日期': str(margin.iloc[-1, 0])}


def mainline(root: Path, h: dict) -> dict:
    latest = read_json(root / 'data/主线/阶段_最新.json') or {}
    table = pd.read_csv(root / 'data/主线/阶段日表_31.csv', dtype={'code': str, 'date': str})
    table = table.drop_duplicates(['date', 'code'], keep='last')
    days = sorted(table.date.unique())[-20:]
    recent = table[table.date.isin(days)].pivot(index='code', columns='date', values='阶段')
    rows = []
    for r in latest.get('行业', []):
        r = dict(r)
        r['近20日'] = recent.loc[r['code']].tolist() if r['code'] in recent.index else []
        rows.append(r)
    alert = root / 'data/主线/提醒_最新.md'
    return {'数据日期': latest.get('数据日期'), '过期': stale(h, 'mainline_signals', 'mainline_stage'),
            '参数版本': latest.get('参数版本'), '行业': rows, '近5日阶段变化': latest.get('近5日阶段变化', []),
            '近20日日期': days, '提醒': alert.read_text(encoding='utf-8') if alert.exists() else '',
            '回放要点': '按草案规则，历史上各阶段进入后都不比基线好，"确认"之后20日偏弱（中位数-1.1%，胜率40%）。'
                       '阶段只当地图和风险提示，"确认""拥挤"不是追的时候。详见 logs/主线回放.md。'}


def flow(root: Path, h: dict) -> dict:
    cols = ['date', 'code', 'name', '占比5日变化_28', '占比5日变化_31', '相对沪深300_20']
    df = pd.read_csv(root / 'data/资金轮动/日表.csv', usecols=cols, dtype={'code': str, 'date': str})
    df = df.drop_duplicates(['date', 'code'], keep='last')
    days = sorted(df.date.unique())[-60:]
    df = df[df.date.isin(days)]
    out = {'数据日期': days[-1] if days else None, '过期': stale(h, 'flow'), '日期': days, '行业': {}}
    for scope in ('28', '31'):
        p = df.pivot(index='code', columns='date', values=f'占比5日变化_{scope}').reindex(columns=days)
        names = df.drop_duplicates('code').set_index('code')['name']
        out['行业'][scope] = [{'code': c, '行业': names.get(c, c), '占比5日变化': [None if pd.isna(v) else round(float(v) * 100, 3)
                                                                         for v in p.loc[c]]}
                             for c in p.index if p.loc[c].notna().any()]
    last = df[df.date == days[-1]].sort_values('相对沪深300_20', ascending=False)
    out['强弱排名'] = [{'行业': r['name'], 'code': r['code'], '相对沪深300_20': r['相对沪深300_20']}
                    for _, r in last.iterrows() if pd.notna(r['相对沪深300_20'])]
    return out


def history(root: Path, h: dict) -> dict:
    cmp = read_json(root / 'data/事件库/当前对照.json') or {}
    hs = pd.read_csv(root / 'data/行情库/指数/sh000300.csv', dtype={'date': str}).set_index('date')['close']
    notes = {}
    for name in ('大调整.csv', '大反弹.csv'):
        path = root / 'data/事件库' / name
        if path.exists():
            for r in csv.DictReader(open(path, encoding='utf-8')):
                if r.get('触发事件'):
                    notes[r['事件id']] = {'触发事件': r['触发事件'], '退潮原因': r.get('退潮原因', ''),
                                          '类型': r.get('传导链或上位类型', '')}
    rows = []
    days = list(hs.index)
    for r in (cmp.get('主表排名') or [])[:5]:
        start = r['开始']
        path = []
        if start in hs.index:
            i = days.index(start)
            seg = hs.iloc[i:i + 61]
            path = [round(float(v / seg.iloc[0] - 1) * 100, 2) for v in seg]
        rows.append({'事件id': r['事件id'], '类型': r['类型'], '开始': start, '结束': r['结束'],
                     '距离': r['距离'], '之后走势': path, '判读': notes.get(r['事件id'], {}),
                     '像在哪': r.get('像在哪、不像在哪', '【待判读】')})
    return {'数据日期': cmp.get('data_date'), '过期': stale(h, 'events'), '当前特征': cmp.get('当前特征', {}),
            '最像': rows}


def macro(root: Path, h: dict) -> dict:
    now = dt.datetime.now(BJ).isoformat()
    items = []
    for name in ('中国10年国债', '美国10年国债', '美元兑人民币', '美元指数', '布伦特原油', 'COMEX黄金',
                 '制造业PMI', 'CPI同比', 'PPI同比', 'M1', 'M2'):
        path = root / f'data/宏观/{name}.csv'
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str)
        df = df[df['可见时刻'] <= now].drop_duplicates('date', keep='last')
        if df.empty:
            continue
        last, prev = df.iloc[-1], (df.iloc[-2] if len(df) > 1 else None)
        items.append({'指标': name, '日期': last['date'], '值': float(last['value']),
                      '上期': float(prev['value']) if prev is not None else None})
    derived = pd.read_csv(root / 'data/宏观/派生.csv', dtype=str)
    derived = derived[derived['可见时刻'] <= now]
    change = {k: float(g.iloc[-1]['value']) for k, g in derived.groupby('指标')}
    dates = [i['日期'] for i in items if len(i['日期']) == 10]
    return {'数据日期': max(dates) if dates else None, '过期': stale(h, 'macro'), '指标': items, '20日变化bp': change}


def judgments(root: Path) -> dict:
    rows = []
    for path in sorted((root / 'judgments').glob('*.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                rows.append(json.loads(line))
    stats = {}
    for r in rows:
        key = f"{r.get('module', '')}·{r.get('confidence', '')}"
        s = stats.setdefault(key, {'待验证': 0, '证实': 0, '证伪': 0, '部分': 0, '无法判定': 0, '已被取代': 0})
        s[r.get('status', '待验证')] = s.get(r.get('status', '待验证'), 0) + 1
    for s in stats.values():
        done = s['证实'] + s['证伪'] + s['部分']
        s['命中率'] = (s['证实'] + 0.5 * s['部分']) / done if done else None
    return {'总数': len(rows), '分组': stats}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    out = args.out or root / 'site/data'
    out.mkdir(parents=True, exist_ok=True)
    h = health(root)
    sections = {'market': market, 'mainline': mainline, 'flow': flow, 'history': history, 'macro': macro}
    meta = {'generated_at': dt.datetime.now(BJ).isoformat(), '栏目': {}, 'errors': []}
    for name, func in sections.items():
        try:
            data = func(root, h)
        except Exception as exc:  # 一个栏目失败不影响其他栏目
            data = {'错误': f'{type(exc).__name__}: {exc}', '过期': True}
            meta['errors'].append(f'{name}: {exc}')
        (out / f'{name}.json').write_text(json.dumps(clean(data), ensure_ascii=False) + '\n', encoding='utf-8')
        meta['栏目'][name] = {'数据日期': data.get('数据日期'), '过期': data.get('过期')}
    (out / 'judgments.json').write_text(json.dumps(clean(judgments(root)), ensure_ascii=False) + '\n', encoding='utf-8')
    (out / 'health.json').write_text(json.dumps(clean(h), ensure_ascii=False) + '\n', encoding='utf-8')
    meta['status'] = '部分失败' if meta['errors'] else '正常'
    (out / 'meta.json').write_text(json.dumps(clean(meta), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    total = sum(p.stat().st_size for p in out.glob('*.json'))
    print(f'看板数据：{out}，{total / 1024:.0f} KB；{meta["status"]} {meta["errors"]}', flush=True)
    return 1 if len(meta['errors']) == len(sections) else 0


if __name__ == '__main__':
    raise SystemExit(main())
