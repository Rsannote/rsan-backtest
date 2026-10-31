"""T15 独立核对：不调用 mainline_stage 的计算函数，直接读原始文件，复核 3 次历史"确认"。"""
import csv, json, sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mainline_signal_store as store

P = {k: v['值'] for k, v in json.loads((ROOT / 'system/主线参数.json').read_text(encoding='utf-8'))['参数'].items()}
sig = store.read_signal_table(ROOT, 28)
sig = sig[sig['主线判定'] == '参与']
rel = sig.pivot(index='date', columns='code', values='资金_相对沪深300_20').astype(float)
ratio = sig.pivot(index='date', columns='code', values='资金_占比5均较20均').astype(float)
pct60 = sig.pivot(index='date', columns='code', values='资金_占比60日分位').astype(float)
days = list(rel.index)
raw = {}
with open(ROOT / 'data/资金轮动/日表.csv', encoding='utf-8') as fh:
    for row in csv.DictReader(fh):
        raw[(row['date'], row['code'])] = row['收盘']          # 同键后写覆盖前写 = 取最后一条
hs = {r['date']: float(r['close']) for r in csv.DictReader(open(ROOT / 'data/行情库/指数/sh000300.csv', encoding='utf-8'))}
state = {r['date']: r['结论'] for r in csv.DictReader(open(ROOT / 'logs/市场状态_规则检验_逐日.csv', encoding='utf-8'))}
events = pd.read_csv(sys.argv[1], dtype={'code': str})
conf = events[events['里程碑'].fillna('').str.contains('确认')]
picks = conf.sample(3, random_state=20261101)
lines = ['# T15 独立手算核对（3 次历史确认）', '', '直接读信号表、资金轮动日表、沪深300 原始文件，逐日复核；不调用阶段判定代码。', '',
         '|行业|确认日|启动日|启动前连续满足天数|启动至确认天数|期间掉出前5天数|确认日占比60日分位|大盘|门槛要求天数/分位|20日超额（独立算）|结论|',
         '|---|---|---|---:|---:|---:|---:|---|---|---:|---|']
for e in picks.itertuples():
    code, day = e.code, e.date
    i = days.index(day)
    rank = rel.rank(axis=1, ascending=False, method='first')[code]
    cond = (ratio[code] >= P['启动_占比提升']) & (rank <= P['启动_强弱排名'])
    # 程序声称的本轮启动日（同行业、确认日之前最近一次"启动"里程碑），逐条独立复核
    prior = events[(events.code == code) & (events.date < day) & events['里程碑'].fillna('').str.contains('启动')]
    entry = days.index(prior.date.max())          # 启动里程碑当天 = 第 5 个连续满足日
    start = entry - 4
    streak = int(cond.iloc[start:entry + 1].sum())
    span = i - start + 1
    out_top = int((rank.iloc[start:i + 1] > P['确认_强弱排名']).sum())
    st = state.get(day, '')
    need_days = P['确认_启动天数'] + (P['大盘守_确认天数加'] if st == '守' else 0)
    need_pct = P['确认_占比60日分位'] + (P['大盘守_分位加'] if st == '守' else 0)
    c0, c20 = float(raw[(day, code)]), float(raw[(days[i + 20], code)]) if i + 20 < len(days) else float('nan')
    ex = c20 / c0 - 1 - (hs[days[i + 20]] / hs[day] - 1) if i + 20 < len(days) else float('nan')
    ok = streak == 5 and span >= need_days and out_top <= P['确认_允许掉出天数'] and pct60.at[day, code] >= need_pct
    lines.append(f"|{code}|{day}|{days[start]}|{streak}|{span}|{out_top}|{pct60.at[day, code]:.1f}|{st or '无数据'}|{need_days}/{need_pct}|{ex:+.4%}|{'一致' if ok else '不一致'}|")
lines += ['', '"期间掉出前5天数"从启动第一天数到确认日，规则允许 ≤5。抗跌条件另由单元测试覆盖。', '']
out = ROOT / 'logs/test/T15/手算核对.md'
out.write_text('\n'.join(lines), encoding='utf-8')
print('\n'.join(lines))
