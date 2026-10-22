"""情绪温度计择时：用行为类情绪指标（成交额、融资余额变化、涨跌停差）合成 0–100 的温度。

来源：2026-10-22 对话，讨论散户情绪与股指顶底的关系后提出"情绪温度计"。
数据（复制在本文件夹）：日指标.csv（全A成交额估算、涨停、跌停，2014 起）、融资余额.csv（2010 起），
均为 2026-10-22 从 data/市场状态/ 复制的固定副本。破净股数量仓库里没有，这一版不用。

2026-10-22 看结果之前写死的规则（只跑这一组，不在看结果后调参）：
  温度 T = 下面三项各自在过去 750 个交易日（含当天）里的分位数（0–100）取平均：
    1. 成交额热度：全A成交额 20 日均值；
    2. 杠杆热度：融资余额 60 日变化率（融资余额次日才公布，用前一交易日的值）；
    3. 赚钱效应：（涨停 − 跌停）20 日均值。
  A 冷买热卖沪深300·国债等待：T ≤ 20 → 满仓 510300；T ≥ 80 → 满仓 511260；其余保持。
  B 冷买热卖沪深300·红利等待：同 A，等待期持有 510880。
  A/B 起始：第一天 T < 50 持有 510300，否则持有等待资产。
  C 红利·过热避险：平时满仓 510880；T ≥ 80 → 满仓 511260；之后 T ≤ 50 → 换回 510880。
"""
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
WIN = 750
_T = None


def _pct_rank(s):
    return s.rolling(WIN, min_periods=WIN).apply(lambda x: (x <= x[-1]).mean() * 100, raw=True)


def temperature():
    global _T
    if _T is None:
        d = pd.read_csv(os.path.join(HERE, "日指标.csv"), parse_dates=["date"]).set_index("date").sort_index()
        m = pd.read_csv(os.path.join(HERE, "融资余额.csv"), parse_dates=["date"]).set_index("date").sort_index()
        rz = m["融资余额亿"].reindex(d.index, method="ffill").shift(1)   # 次日公布，滞后一天
        f1 = _pct_rank(d["成交额亿"].rolling(20).mean())
        f2 = _pct_rank(rz.pct_change(60))
        f3 = _pct_rank((d["涨停"] - d["跌停"]).rolling(20).mean())
        _T = pd.concat([f1, f2, f3], axis=1).mean(axis=1, skipna=False).dropna()
    return _T


def temp_on(date):
    t = temperature()
    t = t.loc[: pd.Timestamp(date)]
    return None if t.empty else float(t.iloc[-1])


def _hold(ctx, code):
    if code in ctx.positions and len(ctx.positions) == 1:
        return None
    return {code: 1.0}


def make_switch(wait):
    def target_weights(ctx):
        t = temp_on(ctx.date)
        if t is None:
            return None
        st = ctx.state
        if "mode" not in st:
            st["mode"] = "risk" if t < 50 else "wait"
        if t <= 20:
            st["mode"] = "risk"
        elif t >= 80:
            st["mode"] = "wait"
        return _hold(ctx, "510300" if st["mode"] == "risk" else wait)
    return target_weights


def make_div_guard():
    def target_weights(ctx):
        t = temp_on(ctx.date)
        if t is None:
            return None
        st = ctx.state
        if "mode" not in st:
            st["mode"] = "div"
        if t >= 80:
            st["mode"] = "bond"
        elif t <= 50:
            st["mode"] = "div"
        return _hold(ctx, "510880" if st["mode"] == "div" else "511260")
    return target_weights


def make_hold(code):
    def target_weights(ctx):
        if code in ctx.positions:
            return None
        return {code: 1.0}
    return target_weights
