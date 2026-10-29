"""红利稳健配置·估值版：在红利稳健配置（30/30/20/20，季度再平衡）的基础上，按估值档位调纳指和红利的仓位。

来源：2026-10-29 对话。看了一条讲"标普500百年实际回报按时代划分、高估值之后常有失落十年"的帖子后，
Rsan 提出：股市估值高时少配纳指，红利便宜时多配红利。

2026-10-29 看结果之前写死的规则（只跑这一组，不在看结果后调参）：
  每季度最后一个交易日收盘后定档，次日开盘调仓（与红利稳健配置同一节奏）。
  1. 纳指 513100：美股席勒市盈率（CAPE，标普500，月度，来源 multpl.com）在过去 240 个月（20 年）里的分位数。
     为避免用到未来数据，只用上个月及以前的值。
     分位 ≥ 80% → 20%；分位 ≤ 20% → 40%；其余 → 30%。
  2. 红利 510880：股息率 = 最近一次分红往前 300 天内的每份分红合计（510880 2014 年起每年 1 月分一次，两次间隔可能不到 365 天，用 300 天避免把两年的分红算成一年） ÷ 当日真实收盘价
     （每份分红由后复权因子的跳变算出：分红 = 前收真实价 × (1 − 前因子/新因子)）。
     分位数 = 当日股息率在 2009-03-24（第一次分红）以来全部交易日里的位置，至少要 3 年历史。
     股息率分位 ≥ 80%（便宜）→ 40%；≤ 20%（贵）→ 20%；其余 → 30%。
  3. 黄金 518880 固定 20%。
  4. 十年国债 511260 = 剩下的部分（0%–40%）：股票少配的部分进国债，股票多配的部分从国债出。
  数据不够定档时，用中枢 30/30/20/20。
"""
import os

import numpy as np
import pandas as pd

NAME = "红利稳健配置·估值版"
REQUIRED = ["510880", "513100", "511260", "518880"]
REBALANCE = "quarterly"
WARMUP_DAYS = 0

HERE = os.path.dirname(os.path.abspath(__file__))
_CAPE = None


def cape():
    global _CAPE
    if _CAPE is None:
        d = pd.read_csv(os.path.join(HERE, "美股CAPE.csv"), parse_dates=["date"])
        _CAPE = d.set_index("date")["cape"].sort_index()
    return _CAPE


def nasdaq_pct(date):
    s = cape()
    month_start = pd.Timestamp(date).to_period("M").to_timestamp()
    s = s[s.index < month_start]          # 只用上个月及以前
    s = s.tail(240)
    if len(s) < 240:
        return None
    return float((s <= s.iloc[-1]).mean() * 100)


def div_yield_series(h):
    real = h["close"] / h["factor"]
    f = h["factor"]
    jump = f / f.shift(1)
    ev = jump[jump > 1 + 1e-9].index
    divs = pd.Series({d: float(real.shift(1).loc[d] * (1 - 1 / jump.loc[d])) for d in ev}, dtype=float)
    if divs.empty:
        return pd.Series(dtype=float)
    # 每个交易日：以最近一次分红为锚，往前 300 天内的分红合计
    annual = pd.Series({d: float(divs[(divs.index > d - pd.Timedelta(days=300)) & (divs.index <= d)].sum())
                        for d in divs.index})
    per_day = annual.reindex(real.index, method="ffill")
    return (per_day / real).dropna()


def dividend_pct(ctx):
    y = div_yield_series(ctx.history("510880"))
    if y.empty or (y.index[-1] - y.index[0]).days < 365 * 3:
        return None
    return float((y <= y.iloc[-1]).mean() * 100)


def tier(p, hi_w, lo_w):
    if p is None:
        return 0.30
    if p >= 80:
        return hi_w
    if p <= 20:
        return lo_w
    return 0.30


def target_weights(ctx):
    pn = nasdaq_pct(ctx.date)
    pd_ = dividend_pct(ctx)
    w_ndx = tier(pn, 0.20, 0.40)    # 越贵越少
    w_div = tier(pd_, 0.40, 0.20)   # 股息率越高（越便宜）越多
    w_gold = 0.20
    w_bond = round(1.0 - w_ndx - w_div - w_gold, 4)
    log = ctx.state.setdefault("档位", [])
    log.append((str(ctx.date.date()), None if pn is None else round(pn, 1),
                None if pd_ is None else round(pd_, 1), w_ndx, w_div, w_bond))
    return {"510880": w_div, "513100": w_ndx, "511260": w_bond, "518880": w_gold}
