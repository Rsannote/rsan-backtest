"""托底信号：20只重点宽基ETF单日申赎净流入超过100亿视为"托底日"，跟着买沪深300。

数据：etf20_daily_flow.csv，取自【东财策略】20只重点ETF资金观测看板（etf-fund-flow-board.app.workbuddy.host）
页面 data.js 里的 dailyData（2026-10-15 换仓后的20只篮子，2023-10-09 ~ 2026-10-20，735 个交易日），
2026-10-21 从页面导出，条数、净流入合计、成交额合计与页面逐项核对一致。
净流入为申赎口径（份额变动 × 单位净值），单位亿元。

2026-10-21 回测前先定好的规则（看结果之前写死，不在看结果后调参）：
  1. 托底日：当日 20 只 ETF 净流入 > 100 亿。不剔除看板里那两段"狂热申购窗/抛售窗"——
     那是事后挑出来的，实盘当时没法知道。
  2. 信号何时可用：ETF 份额是收盘后才公布的，T 日的净流入最早在 T+1 日才能看到。
     标准版：T+1 收盘后确认信号，T+2 开盘买入（滞后一天）。
     乐观版：假设 T 日收盘就知道，T+1 开盘买入——只用来看信号本身有没有用，实盘做不到。
  3. 买入后持有 N 个交易日（20 或 60）；持有期内又出现托底日，就从新信号那天重新计 N 天。
  4. 等待期：现金版空仓（现金不计息）；红利底仓版平时满仓红利 510880，托底期内换成沪深300。
"""
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
_flow = pd.read_csv(os.path.join(HERE, "etf20_daily_flow.csv"), parse_dates=["date"]).set_index("date")["netflow_yi"]
THRESHOLD = 100.0
RISK = "510300"
BASE = "510880"


def _signal(date, lag):
    """截至 date 收盘时能看到的、最新一个已公布交易日是否为托底日。"""
    known = _flow.loc[:date]
    if lag:
        known = known[known.index < date]
    if known.empty:
        return False
    last_day = known.index[-1]
    # 只认"刚公布"的那一天，避免同一个托底日被重复计入
    return bool(known.iloc[-1] > THRESHOLD), last_day


def make(hold_days, lag=True, base=None):
    def target_weights(ctx):
        st = ctx.state
        res = _signal(ctx.date, lag)
        fresh = False
        if res:
            is_sup, day = res
            if is_sup and st.get("last_sig_day") != day:
                st["last_sig_day"] = day
                fresh = True
        if fresh:
            st["left"] = hold_days
        else:
            st["left"] = st.get("left", 0) - 1

        want = RISK if st["left"] > 0 else base
        target = {want: 1.0} if want else {}
        # 已经是目标持仓就不动（避免每天再平衡）；没买进（停牌等）就继续挂单
        if want and want in ctx.positions and len(ctx.positions) == 1:
            return None
        if not want and not ctx.positions:
            return None
        return target
    return target_weights


def make_hold(code):
    def target_weights(ctx):
        if code in ctx.positions:  # 已买进就不再动
            return None
        return {code: 1.0}
    return target_weights
