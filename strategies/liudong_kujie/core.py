"""流动性枯竭抄底：等下跌中的"流动性枯竭"出现、指数止跌后再买沪深300ETF。

来源：2026-10-21 对话里讨论的交易心得第6条"等待流动性枯竭"。
数据：沪深300指数（价格信号）、上证指数成交量（代表全市场流动性；成交量为股数口径，只用比值）。
跌停家数、融资余额、300ETF期权隐含波动率仓库里没有数据，这一版先不用。

2026-10-21 看结果之前写死的规则（只跑这一组，不在看结果后调参）：
  1. 下跌前提：近 40 个交易日内，沪深300收盘曾低于近 250 日最高收盘的 85%（回撤 ≥15%）。
  2. 流动性事件（近 20 个交易日内出现过，且发生在上次卖出之后）：
     A 缩量枯竭：上证 5 日均量 ≤ 60 日均量 × 0.65；
     B 放量踩踏：某日沪深300跌幅 ≥3%，且上证当日成交量 ≥ 60 日均量 × 1.5。
  3. 止跌确认：沪深300收盘站上 20 日均线。
  1+2+3 同时满足 → 次日开盘满仓买入 510300。
  卖出（任一满足，次日开盘卖）：
     - 止盈：沪深300收盘回到买入时的前高（买入当天近 250 日最高收盘）；
     - 止损：沪深300收盘跌破买入当天收盘的 92%；
     - 时间：持有满 120 个交易日（约 6 个月）。
  4. 等待期：现金版空仓（现金不计息）；红利底仓版平时满仓红利 510880。
"""
import pandas as pd

IDX = "000300"  # 沪深300指数（引擎内部去掉 sh 前缀）
MKT = "000001"  # 上证指数
RISK = "510300"
BASE = "510880"


def _liquidity_event(ctx, mode, last_exit):
    idx = ctx.history(IDX, 100)
    mkt = ctx.history(MKT, 100)
    if len(idx) < 81 or len(mkt) < 81:
        return False
    v = mkt["volume"]
    ma60 = v.rolling(60).mean()
    if mode == "A":
        hit = v.rolling(5).mean() <= ma60 * 0.65
    else:
        ret = idx["close"].pct_change()
        ret = ret.reindex(v.index)
        hit = (ret <= -0.03) & (v >= ma60 * 1.5)
    recent = hit.iloc[-20:]
    days = recent[recent].index
    if last_exit is not None:
        days = [d for d in days if pd.Timestamp(d) > pd.Timestamp(last_exit)]
    return len(days) > 0


def _setup(ctx):
    h = ctx.history(IDX, 300)
    if len(h) < 291:
        return None
    c = h["close"]
    peak = c.rolling(250).max()
    dd_ok = (c <= peak * 0.85).iloc[-40:].any()
    stable = c.iloc[-1] > c.iloc[-20:].mean()
    return dd_ok and stable, float(c.iloc[-250:].max()), float(c.iloc[-1])


def make(mode, base=None):
    def target_weights(ctx):
        st = ctx.state
        close = float(ctx.history(IDX, 1)["close"].iloc[-1])
        if st.get("in"):
            st["held"] += 1
            if close >= st["target"] or close <= st["stop"] or st["held"] >= 120:
                st["in"] = False
                st["last_exit"] = ctx.date
        else:
            s = _setup(ctx)
            if s and s[0] and _liquidity_event(ctx, mode, st.get("last_exit")):
                st.update({"in": True, "held": 0, "target": s[1], "stop": s[2] * 0.92})
        want = RISK if st.get("in") else base
        if want:
            if want in ctx.positions and len(ctx.positions) == 1:
                return None
            return {want: 1.0}
        return None if not ctx.positions else {}
    return target_weights


def make_hold(code):
    def target_weights(ctx):
        if code in ctx.positions:
            return None
        return {code: 1.0}
    return target_weights
