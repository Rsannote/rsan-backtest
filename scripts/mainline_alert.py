#!/usr/bin/env python3
"""主线雷达 · 共振型启动提醒（2026-11-01 Rsan 决定纳入提醒范围）。

读 data/主线/阶段事件_31.csv，找出"进入启动且上位类型=共振"的事件；没提醒过的，
开一个 GitHub Issue（标签"主线提醒"），手机 GitHub App 会收到通知。

依据：T15 回放里，历史主线案例中"共振型"成立后 60 日胜率较高，但样本只有 11 个、类型由 Claude 事后补写，
只能当线索。所以这里只做提醒，不是买卖信号。

- 已提醒过的事件记在 data/主线/提醒记录.csv（只追加），不重复开 Issue。
- 只提醒最近 N 个交易日内的事件（默认 5），避免首次运行把历史全部翻出来。
- 本地运行（不在 Actions）只打印、只写文件，不联网。

用法：python3 scripts/mainline_alert.py [--days 5] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
BJ = dt.timezone(dt.timedelta(hours=8))
LABEL = '主线提醒'
LOG_FIELDS = ['date', 'code', '行业', '上位类型', 'issue', '记录时间']


def pending(root: Path, days: int) -> tuple[list[dict], set]:
    events = pd.read_csv(root / 'data/主线/阶段事件_31.csv', dtype={'code': str, 'date': str})
    if events.empty:
        return [], set()
    events = events[events['里程碑'].fillna('').str.contains('启动') & (events['上位类型'] == '共振')]
    stage_days = sorted(pd.read_csv(root / 'data/主线/阶段事件_31.csv', usecols=['date'], dtype=str)['date'].unique())
    latest = json.loads((root / 'data/主线/阶段_最新.json').read_text(encoding='utf-8'))
    all_days = sorted(set(stage_days) | {latest['数据日期']})
    cutoff = all_days[max(0, len(all_days) - days)]
    log = root / 'data/主线/提醒记录.csv'
    done = set()
    if log.exists():
        for row in csv.DictReader(open(log, encoding='utf-8')):
            done.add((row['date'], row['code']))
    todo = [r for r in events.to_dict('records') if r['date'] >= cutoff and (r['date'], r['code']) not in done]
    return todo, done


def describe(root: Path, event: dict) -> tuple[str, str]:
    latest = json.loads((root / 'data/主线/阶段_最新.json').read_text(encoding='utf-8'))
    info = next((r for r in latest['行业'] if r['code'] == event['code']), {})
    name = info.get('行业') or event['code']
    title = f"[主线提醒] {event['date']} {name}：进入启动（共振型）"
    def pct(x):
        return '—' if x is None else f'{x * 100:.1f}%'
    body = '\n'.join([
        f"**{name}**（{event['code']}）在 {event['date']} 进入**启动**，上位类型为**共振**：利率环境利好它的久期，同时近 10 个交易日有重要度 3 的新闻或催化日历事件。",
        '',
        f"- 当前阶段：{info.get('阶段', '—')}（数据日期 {latest['数据日期']}）",
        f"- 20 日相对强弱排名：{info.get('20日强弱排名', '—')}",
        f"- 成交占比 5 日均较 20 日均：{pct(info.get('占比5日均较20日均'))}",
        f"- 占比一年分位：{'—' if info.get('占比一年分位') is None else round(info['占比一年分位'], 1)}",
        f"- 大盘：{event.get('gate') or '—'}",
        '',
        '为什么提醒：T15 回放中，历史主线案例里"共振型"成立后 60 日表现较好，但样本只有 11 个，且类型是事后补写的，只能当线索。'
        '同时回放显示，"确认"之后整体偏弱，所以**这不是买入信号**，是请你和 Claude 一起看一眼：催化够不够硬、和历史哪次像、要盯什么。',
        '',
        '详见 `data/主线/阶段_最新.md`、`logs/主线回放.md`。程序按规则生成，不构成投资建议。',
    ])
    return title, body


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--days', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    todo, _ = pending(root, args.days)
    now = dt.datetime.now(BJ).isoformat()
    online = os.environ.get('GITHUB_ACTIONS') == 'true' and not args.dry_run
    api = None
    if online and todo:
        from health_alert import GitHub
        api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ.get('GITHUB_TOKEN', ''))
        try:
            api.request('GET', 'labels/' + __import__('urllib.parse').parse.quote(LABEL, safe=''))
        except Exception:
            api.request('POST', 'labels', {'name': LABEL, 'color': '0E8A16', 'description': '主线雷达：共振型进入启动'})
    rows, lines = [], ['# 主线提醒', '', f'生成：{now}（北京时间）。只提醒"进入启动且上位类型=共振"，不是买卖信号。', '']
    for event in todo:
        title, body = describe(root, event)
        number = ''
        if api:
            issue = api.request('POST', 'issues', {'title': title, 'body': body, 'labels': [LABEL]})
            number = str(issue.get('number', ''))
        print(title + (f'（Issue #{number}）' if number else '（未联网，只记录）'), flush=True)
        lines.append(f'- {title}' + (f'（Issue #{number}）' if number else ''))
        name = title.split(' ')[2].split('：')[0]
        rows.append({'date': event['date'], 'code': event['code'], '行业': name, '上位类型': event['上位类型'],
                     'issue': number, '记录时间': now})
    if not todo:
        lines.append('最近没有新的共振型启动。')
    (root / 'data/主线/提醒_最新.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if rows and online:          # 只有真正开了 Issue 才记为已提醒；本地运行不记，避免漏发
        log = root / 'data/主线/提醒记录.csv'
        new = not log.exists()
        with open(log, 'a', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS, lineterminator='\n')
            if new:
                writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
