"""财报惊喜（A版）：财报公告后市场用价格确认"超预期"，跟进持有。

规则在看结果之前写死（2026-10-21，对话里和 Rsan 定好）：
  1. 范围：命脉股票池 123 只（含第一层已淘汰的，减少幸存者偏差），排除科创板、北交所。
     按业绩加速第一层近似筛（只用公告日之前已披露的年报）：最近三个年报净利润为正；
     经营现金流为正（金融除外）；最近年报 ROE ≥ 8%；市值 ≥ 150 亿（股价 × 净利润/每股收益 估算总股本）；
     近 60 个交易日日均成交额 ≥ 0.5 亿；上市满 5 年（行情库 2005 年起，更早上市的按 2005 年算）。
  2. 公告日 T：财报公告日当天或之后的第一个交易日（盘后披露的，反应在下一交易日，
     窗口从 T-1 收盘量起，多算半天不影响，也不会用到未来数据）。
  3. 触发（T+2 收盘后判断，三条同时满足）：
     ① 单季扣非净利润同比 ≥ 20%（去年同季为正）；
     ② T 到 T+2 累计涨幅（T+2 收盘 / T-1 收盘）减沪深300 同期涨幅 ≥ 阈值（主版本 5%）；
     ③ T+2 收盘高于 T-1 收盘（缺口没回补）。
  4. 买入：T+3 开盘（引擎：T+2 收盘出信号，次日开盘成交）。
  5. 持有：满 N 个交易日（主版本 60）卖出；或者下一份财报的 T+2 没有再次触发就卖出，先到为准；
     再次触发则从这天重新计 N 天。中途不止损、不止盈。
  6. 仓位：最多 K 只（100 万用 10 只，2 万用 5 只），每只 1/K；同一行业最多 1 只；
     空位不够时按三日超额收益从高到低。已持有的不再平衡；空余资金为现金（不计息）。
"""
import os
import math

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = "000300"
FIN = {"金融"}

_q = pd.read_csv(os.path.join(HERE, "caibao.csv"), dtype={"code": str, "报告期": str, "公告日": str})
_q["code"] = _q["code"].str.zfill(6)
_y = pd.read_csv(os.path.join(HERE, "nianbao.csv"), dtype={"code": str, "报告期": str})
_y["code"] = _y["code"].str.zfill(6)
_ind = pd.read_csv(os.path.join(HERE, "hangye.csv"), dtype={"code": str})
_ind["code"] = _ind["code"].str.zfill(6)
INDUSTRY = dict(zip(_ind["code"], _ind["行业"]))
POOL = [c for c in INDUSTRY if not c.startswith(("688", "4", "8", "9"))]

# 单季数据：code -> {报告期: (扣非, 公告日)}
Q = {}
for r in _q.itertuples(index=False):
    kf = r.扣非净利润
    if not (isinstance(kf, float) and math.isfinite(kf)):
        kf = r.净利润
    Q.setdefault(r.code, {})[r.报告期] = (kf, (r.公告日 if isinstance(r.公告日, str) and len(r.公告日) >= 10 else None))
# 公告列表：(公告日, code, 报告期)
EVENTS = sorted((v[1], c, p) for c, d in Q.items() for p, v in d.items() if v[1] and p >= "2009-01-01")
EVENT_DATES = pd.to_datetime([e[0] for e in EVENTS])
# 年报：code -> {年份: dict}
Y = {}
for r in _y.itertuples(index=False):
    Y.setdefault(r.code, {})[int(r.报告期)] = dict(np=r.净利润, ocf=r.每股经营现金流, roe=r.净资产收益率, eps=r.基本每股收益)


def _yoy(c, p):
    d = Q.get(c, {})
    if p not in d:
        return math.nan
    prev = f"{int(p[:4]) - 1}{p[4:]}"
    if prev not in d:
        return math.nan
    a, b = d[p][0], d[prev][0]
    if not (isinstance(b, float) and b > 0 and isinstance(a, float) and math.isfinite(a)):
        return math.nan
    return a / b - 1


def _layer1(ctx, c, today):
    """只用今天之前已公告的年报。"""
    d = Q.get(c, {})
    years = sorted(int(p[:4]) for p, v in d.items() if p.endswith("12-31") and v[1] and pd.Timestamp(v[1]) < today)
    if not years:
        return False
    yr = years[-1]
    ys = Y.get(c, {})
    need = [yr - 2, yr - 1, yr]
    if any(y not in ys for y in need):
        return False
    rows = [ys[y] for y in need]
    if not all(isinstance(x["np"], float) and x["np"] > 0 for x in rows):
        return False
    if INDUSTRY.get(c) not in FIN and not all(isinstance(x["ocf"], float) and x["ocf"] > 0 for x in rows):
        return False
    roe = rows[-1]["roe"]
    if not (isinstance(roe, float) and roe >= 0.08):
        return False
    h = ctx.history(c, 60)
    if len(h) < 60 or "volume" not in h.columns:
        return False
    raw = h["close"] / h["factor"]
    if float((h["volume"] * 100 * raw).mean()) < 0.5e8:
        return False
    eps = rows[-1]["eps"]
    if not (isinstance(eps, float) and eps > 0):
        return False
    mcap = float(raw.iloc[-1]) * rows[-1]["np"] / eps
    if mcap < 150e8:
        return False
    first = ctx.history(c).index[0]
    if (today - first).days < 5 * 365 and first > pd.Timestamp("2005-01-10"):
        return False
    return True


def _events_today(ctx):
    """今天收盘正好是 T+2 的公告：返回 [(code, 报告期, T-1日期)]。"""
    idx = ctx.history(INDEX, 12)
    if len(idx) < 5:
        return []
    days = idx.index
    today = days[-1]
    out = []
    lo = EVENT_DATES.searchsorted(days[-5] - pd.Timedelta(days=10))
    hi = EVENT_DATES.searchsorted(today, side="right")
    for k in range(lo, hi):
        pub, c, p = EVENTS[k]
        pub = pd.Timestamp(pub)
        after = days[days >= pub]
        if len(after) == 3 and after[-1] == today:       # T、T+1、T+2=今天
            before = days[days < pub]
            if len(before):
                out.append((c, p, before[-1]))
    return out


def _excess(ctx, c, d0):
    """T-1 收盘到今天收盘：个股涨幅 - 沪深300 涨幅；以及缺口是否保持。"""
    h = ctx.history(c, 12)
    i = ctx.history(INDEX, 12)
    if d0 not in h.index or d0 not in i.index or h.index[-1] != i.index[-1]:
        return None, False
    r_s = h["close"].iloc[-1] / h.at[d0, "close"] - 1
    r_i = i["close"].iloc[-1] / i.at[d0, "close"] - 1
    return r_s - r_i, h["close"].iloc[-1] > h.at[d0, "close"]


def make(threshold=0.05, hold_days=60, slots=10):
    def target_weights(ctx):
        st = ctx.state
        held = st.setdefault("held", {})       # code -> 剩余交易日
        today = ctx.date
        # 0) 同步：没买进的从记录里去掉（停牌、买不起一手）
        for c in list(held):
            if held[c] == "pending":
                if c in ctx.positions:
                    held[c] = hold_days
                else:
                    held.pop(c)
            elif c not in ctx.positions:
                held.pop(c)
        # 1) 持有天数
        sells = set()
        for c in list(held):
            held[c] -= 1
            if held[c] <= 0:
                sells.add(c)
        # 2) 今天是 T+2 的公告
        signals = []
        for c, p, d0 in _events_today(ctx):
            if c not in POOL:
                continue
            g = _yoy(c, p)
            ex, gap = _excess(ctx, c, d0)
            ok = ex is not None and g == g and g >= 0.20 and ex >= threshold and gap
            if c in held:
                if ok:
                    held[c] = hold_days; sells.discard(c)    # 再次触发：重新计天数
                else:
                    sells.add(c)                             # 下一份财报没再触发：卖出
                continue
            if ok and _layer1(ctx, c, today):
                signals.append((ex, c))
        if not sells and not signals:
            return None
        keep = {c: w for c, w in ctx.weights().items() if c not in sells}
        for c in sells:
            held.pop(c, None)
        used_ind = {INDUSTRY.get(c) for c in keep}
        free = slots - len(keep)
        new = []
        for ex, c in sorted(signals, reverse=True):
            if free <= 0:
                break
            if INDUSTRY.get(c) in used_ind:
                continue
            new.append(c); used_ind.add(INDUSTRY.get(c)); free -= 1
        if not sells and not new:
            return None
        room = max(0.0, 1.0 - sum(keep.values()))
        w = dict(keep)
        for c in new:
            w[c] = min(1.0 / slots, room / len(new))
            held[c] = "pending"
        return w
    return target_weights
