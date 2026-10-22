#!/usr/bin/env python3
"""市场状态规则检验：用历史数据按 scripts/market_state.py 的规则逐日算出攻守和冷热，
统计之后 5/20/60 个交易日沪深300 的涨跌、最大回撤和上涨概率，看规则有没有区分度。

    python3 scripts/market_state_check.py [--since 2016-01-01]

只检验大盘部分（板块部分的行业ETF历史太短）。结果写 logs/市场状态_规则检验.md。
注意：这只是看结论和后续走势有没有关系，不是交易回测；要拿它当仓位开关，必须另按固定口径回测并跑赢红利。
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market_state as M  # noqa: E402


def fwd_stats(c, i, n):
    if i + n >= len(c):
        return None, None
    seg = c.iloc[i:i + n + 1].values
    ret = seg[-1] / seg[0] - 1
    dd = (seg / np.maximum.accumulate(seg) - 1).min()
    return ret * 100, dd * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2016-01-01")
    a = ap.parse_args()
    hs = M.read_hq("指数", "sh000300")
    ind = M.read_ind()
    for c in ("成交额亿", "涨停", "跌停", "炸板", "最高连板"):
        ind[c] = pd.to_numeric(ind[c], errors="coerce")
    margin = pd.read_csv(M.MARGIN, dtype={"date": str})
    dates = [d for d in hs.index if d >= a.since and d in set(ind["date"])]
    c = hs["close"]
    pos = {d: i for i, d in enumerate(hs.index)}
    rows = []
    for d in dates:
        mk = M.market_part(d, ind, hs, margin)
        r = {"date": d, "结论": mk["结论"], "冷热": mk["冷热"].split("（")[0], "总分": mk["总分"],
             "有两融": mk["两融"] is not None,
             "旧版": mk["旧版对照"]["结论"]}
        for n in (5, 20, 60):
            r[f"{n}日涨跌"], r[f"{n}日最大回撤"] = fwd_stats(c, pos[d], n)
        rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(M.ROOT, "logs", "市场状态_规则检验_逐日.csv"), index=False)

    def table(key, order):
        L = [f"| {key} | 天数 | 占比 | 之后5日均涨跌 | 之后20日均涨跌 | 20日上涨概率 | 20日内平均最大回撤 | 20日内回撤超5%概率 | 之后60日均涨跌 |",
             "|---|---|---|---|---|---|---|---|---|"]
        for g in order + ["全部"]:
            s = df if g == "全部" else df[df[key] == g]
            s20 = s.dropna(subset=["20日涨跌"])
            if s.empty:
                continue
            L.append(f"| {g} | {len(s)} | {len(s) / len(df) * 100:.0f}% | {s['5日涨跌'].mean():+.2f}% | "
                     f"{s20['20日涨跌'].mean():+.2f}% | {(s20['20日涨跌'] > 0).mean() * 100:.0f}% | "
                     f"{s20['20日最大回撤'].mean():.2f}% | {(s20['20日最大回撤'] < -5).mean() * 100:.0f}% | "
                     f"{s['60日涨跌'].mean():+.2f}% |")
        return L

    # 切换次数（看会不会来回跳）
    sw = (df["结论"] != df["结论"].shift()).sum() - 1
    sw_old = (df["旧版"] != df["旧版"].shift()).sum() - 1
    years = (pd.Timestamp(df["date"].iloc[-1]) - pd.Timestamp(df["date"].iloc[0])).days / 365.25
    L = ["# 市场状态规则检验（云端v1）", "",
         f"窗口：{df['date'].iloc[0]} 至 {df['date'].iloc[-1]}，{len(df)} 个交易日。沪深300 为价格指数。",
         "成交额、涨跌停、炸板在接入实时数据之前是 qlib 回补估算；融资余额只有东财能翻到的年份，之前的日子该信号记 0。",
         f"结论切换 {sw} 次，平均每年 {sw / years:.1f} 次；旧版 {sw_old} 次，每年 {sw_old / years:.1f} 次。", "",
         "这里只看规则结论和之后走势有没有关系，不是交易回测，也不构成投资建议。", "",
         "## 攻守", ""] + table("结论", ["攻", "中性", "守"]) + \
        ["", "## 旧版（Mac 股票助理）进守空对照", ""] + table("旧版", ["进", "守", "空"]) + \
        ["", "## 冷热", ""] + table("冷热", ["过热", "偏热", "正常", "偏冷", "过冷"]) + \
        ["", "## 分年份的攻守天数", "", "| 年 | 攻 | 中性 | 守 | 沪深300全年涨跌 |", "|---|---|---|---|---|"]
    df["年"] = df["date"].str[:4]
    for y, s in df.groupby("年"):
        cy = c[(c.index >= f"{y}-01-01") & (c.index <= f"{y}-12-31")]
        L.append(f"| {y} | {(s['结论'] == '攻').sum()} | {(s['结论'] == '中性').sum()} | {(s['结论'] == '守').sum()} | "
                 f"{(cy.iloc[-1] / cy.iloc[0] - 1) * 100:+.1f}% |")
    with open(os.path.join(M.ROOT, "logs", "市场状态_规则检验.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
