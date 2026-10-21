"""月周共振：月线定方向，周线找买卖点，日线不看。

口诀：月线跌到位、周线由跌转横 → 买；月线周线转涨后，周线由涨变高位横盘 → 卖；中途不理震荡。
2026-10-21 回测前先定好的量化定义（看结果之前写死，不在看结果后调参）：

买入（空仓时，每周最后一个交易日收盘判断，次日开盘全仓买入）
  1. 月线跌到位：月收盘较近 60 个月最高月收盘回撤 ≥ 阈值（标准版 40%，宽松版 30%），
     且月线 MACD(12,26,9) 柱子为负、比上个月缩短（绿柱缩短）。
     满足后"到位"状态保留 26 周，等周线筑底。
  2. 周线由跌转横：近 26 周的最低点出现在 8 周以前（连续 8 周不创新低），
     且周线 20 均线近 4 周变化不超过 ±2%（走平）。

卖出（持仓时，每周判断）
  止损：周收盘跌破买入时近 26 周的最低点（横盘区间下沿）。
  止盈：同时满足 ① 买入后最高周收盘 ≥ 买入价 × 1.5；② 连续 6 周没有再创新高（高位横盘）；
        ③ 月线 12 均线向上、周线 20 均线近 4 周向上（月周都处于上升趋势）。
  除此之外一律持有，不管中途震荡。

价格一律用后复权（分红再投资），只用到当天收盘为止的数据。
"""
import pandas as pd

ARM_WEEKS = 26
BASE_WEEKS = 8
FLAT_TOL = 0.02
GAIN_NEED = 1.5
TOP_WEEKS = 6
MIN_MONTHS = 40


def _bars(h, period):
    g = h.groupby(h.index.to_period(period))
    return pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})


def _macd_hist(s):
    dif = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif - dea


def make(code, dd_th):
    def target_weights(ctx):
        st = ctx.state
        h = ctx.history(code)
        if h.empty:
            return None
        m = _bars(h, "M")
        w = _bars(h, "W")
        if len(m) < MIN_MONTHS or len(w) < 40:
            return {}
        wk = len(w)  # 用周数当时钟

        # 月线跌到位（状态保留 ARM_WEEKS 周）
        mc = m["close"]
        dd = mc.iloc[-1] / mc.tail(60).max() - 1
        hist = _macd_hist(mc)
        if dd <= -dd_th and hist.iloc[-1] < 0 and hist.iloc[-1] > hist.iloc[-2]:
            st["armed_at"] = wk
        armed = "armed_at" in st and wk - st["armed_at"] <= ARM_WEEKS

        wc = w["close"]
        ma20 = wc.rolling(20).mean()
        ma_chg = ma20.iloc[-1] / ma20.iloc[-5] - 1

        if not st.get("holding"):
            lows = w["low"].tail(ARM_WEEKS)
            pos_low = len(lows) - 1 - int(lows.values.argmin())  # 最低点距今几周
            base_ok = pos_low >= BASE_WEEKS and abs(ma_chg) <= FLAT_TOL
            if armed and base_ok:
                st.update(holding=True, entry=float(wc.iloc[-1]), stop=float(lows.min()),
                          peak=float(wc.iloc[-1]), peak_at=wk)
                st.pop("armed_at", None)
                return {code: 1.0}
            return {}

        # 持仓中（买入那天停牌没成交的，继续挂单直到买进）
        if code not in ctx.positions:
            return {code: 1.0}
        c = float(wc.iloc[-1])
        if c > st["peak"]:
            st["peak"], st["peak_at"] = c, wk
        if c < st["stop"]:
            st["holding"] = False
            return {}
        ma12m = mc.rolling(12).mean()
        trend_up = ma12m.iloc[-1] > ma12m.iloc[-2] and ma_chg > 0
        if st["peak"] >= st["entry"] * GAIN_NEED and wk - st["peak_at"] >= TOP_WEEKS and trend_up:
            st["holding"] = False
            return {}
        return None  # 继续持有，不做再平衡

    return target_weights


def make_hold(code):
    def target_weights(ctx):
        if code in ctx.positions:  # 已买进就不再动（停牌没买到的，下周继续买）
            return None
        return {code: 1.0}

    return target_weights
