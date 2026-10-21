"""波段抄底（原方案v1）左侧原版 vs 右侧确认版。

起因：国投策略《战无不胜》报告称 2019 年后 A 股左侧买入（重仓前 120 日下跌）明显不如右侧（重仓前 120 日上涨）。
规则在看结果之前写死（2026-10-21）：

  范围：命脉股票池 123 只（含第一层已淘汰的），排除科创板、北交所；上市满 250 个交易日；
        近 60 日日均成交额 ≥ 0.5 亿。
  仓位：最多 6 只，每只 1/6；同一行业最多 1 只；已持有的不再平衡；空余为现金。
  卖出（三个版本完全相同，只改买点）：收盘 ≥ 目标价（前高与买入价+20% 取低者）止盈；
        收盘 ≤ 买入价×0.92 止损；持有满 126 个交易日（约 6 个月）卖出。买入价 = 成交日开盘价。
        卖出后 20 个交易日内不再买同一只。
  买点：
    左侧原版：距 250 日最高收盘回落 ≥ 20%，且收盘 ≤ 60 日最低收盘×1.03（跌到近期支撑）。前高 = 250 日最高。
    右侧回调版：近 120 日涨幅 > 0；距 60 日最高回落 ≥ 10%；今天收盘重新站上 20 日均线（昨天在下方）。前高 = 60 日最高。
    右侧突破版：近 120 日涨幅 > 0；收盘创 250 日新高。前高即当前价，目标只用 +20%。
  候选多于空位：按到目标价的空间从大到小（止损固定 8%，等于按赚亏比）；同分按 120 日涨幅从高到低。
"""
import os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
_ind = pd.read_csv(os.path.join(HERE, "hangye.csv"), dtype={"code": str})
_ind["code"] = _ind["code"].str.zfill(6)
INDUSTRY = dict(zip(_ind["code"], _ind["行业"]))
POOL = [c for c in INDUSTRY if not c.startswith(("688", "4", "8", "9"))]

SLOTS, STOP, UP, MAXHOLD, COOL = 6, 0.08, 0.20, 126, 20


def _signal(mode, h):
    """h：截至今天的 260 日历史（后复权）。返回 (是否买, 前高) 。"""
    c = h["close"]
    px = float(c.iloc[-1])
    r120 = px / float(c.iloc[-121]) - 1
    if mode == "left":
        hi = float(c.iloc[-250:].max())
        lo60 = float(c.iloc[-60:].min())
        return (px <= hi * 0.80 and px <= lo60 * 1.03), hi, r120
    if mode == "pullback":
        hi = float(c.iloc[-60:].max())
        ma = c.rolling(20).mean()
        ok = r120 > 0 and px <= hi * 0.90 and px > float(ma.iloc[-1]) and float(c.iloc[-2]) <= float(ma.iloc[-2])
        return ok, hi, r120
    if mode == "breakout":
        ok = r120 > 0 and px >= float(c.iloc[-251:-1].max())
        return ok, px, r120
    raise ValueError(mode)


def make(mode):
    def target_weights(ctx):
        st = ctx.state
        held = st.setdefault("held", {})      # code -> dict(entry, target_hi, days)
        cool = st.setdefault("cool", {})      # code -> 剩余冷却天数
        for c in list(cool):
            cool[c] -= 1
            if cool[c] <= 0:
                cool.pop(c)
        pos = ctx.weights()
        # 补登成交：昨天出信号、今天开盘成交的
        for c, d in list(held.items()):
            if d["entry"] is None:
                if c in pos:
                    op = float(ctx.history(c, 1)["open"].iloc[-1])
                    d["entry"] = op
                    d["target"] = min(d["hi"], op * (1 + UP)) if d["hi"] > op * 1.001 else op * (1 + UP)
                else:
                    held.pop(c)               # 没买进（停牌等），作废
        sells = set()
        for c, d in held.items():
            if d["entry"] is None:
                continue
            d["days"] += 1
            px = ctx.close(c)
            if px is None:
                continue
            if px >= d["target"] or px <= d["entry"] * (1 - STOP) or d["days"] >= MAXHOLD:
                sells.add(c)
        keep = {c: w for c, w in pos.items() if c not in sells}
        for c in sells:
            held.pop(c, None); cool[c] = COOL
        free = SLOTS - len(keep)
        new = []
        if free > 0:
            used = {INDUSTRY.get(c) for c in keep}
            cands = []
            for c in POOL:
                if c in keep or c in cool or c not in ctx.universe:
                    continue
                h = ctx.history(c, 260)
                if len(h) < 252:
                    continue
                raw = h["close"].iloc[-60:] / h["factor"].iloc[-60:]
                if float((h["volume"].iloc[-60:] * 100 * raw).mean()) < 0.5e8:
                    continue
                ok, hi, r120 = _signal(mode, h)
                if not ok:
                    continue
                px = float(h["close"].iloc[-1])
                room = min(hi / px - 1, UP) if hi > px * 1.001 else UP
                cands.append((room, r120, c, hi))
            for room, r120, c, hi in sorted(cands, reverse=True):
                if free <= 0:
                    break
                if INDUSTRY.get(c) in used:
                    continue
                new.append((c, hi)); used.add(INDUSTRY.get(c)); free -= 1
        if not sells and not new:
            return None
        w = dict(keep)
        room_w = max(0.0, 1.0 - sum(keep.values()))
        for c, hi in new:
            w[c] = min(1.0 / SLOTS, room_w / len(new))
            held[c] = {"entry": None, "hi": hi, "days": 0, "target": None}
        return w
    return target_weights
