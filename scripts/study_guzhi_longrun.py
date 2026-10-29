"""参考研究：红利稳健配置·估值版 的思路放到美股长窗口（1972 起）里检验，覆盖两段美股失落十年。

这不是固定口径回测：没有 ETF、没有 A 股红利，用的是月度指数级数据，不计交易费用，
只用来回答"估值档位在坏的十年里有没有用"。结果不和红利 510880、红利稳健配置做正式比较。

2026-10-29 看结果之前写定的规则（只跑这几组）：
  资产：美股＝标普500全收益（席勒月均价 + multpl 股息率按月计入）；
        美债＝十年期国债按月收益率换算的平价债券收益（按上月收益率做票息、本月收益率重新定价，剩余期限 9 年 11 个月）；
        黄金＝伦敦金月度价格。
  每季度末（3/6/9/12 月）定档，下个月起生效。
  估值：席勒市盈率在过去 240 个月里的分位，只用上个月及以前的值。
  A 固定配置：股 60 / 债 20 / 金 20（对应红利稳健配置里红利+纳指合计六成股票）
  B 估值版±10：分位 ≥80 → 股 50 / 债 30；分位 ≤20 → 股 70 / 债 10；其余 股 60 / 债 20；金固定 20（与已测的估值版同幅度）
  C 估值版±20：分位 ≥80 → 股 40 / 债 40；分位 ≤20 → 股 80 / 债 0；其余同 A；金固定 20（看幅度加大后的效果）
  D 全仓美股：只作参照。
  窗口：1972-01 至 2026-06（黄金 1971 年与美元脱钩后才是市场价；股息率数据到 2026-06）。
  分段：1972–1982、1983–1999、2000–2012、2013–2026-06。名义年化、实际年化（扣 CPI）、最大回撤（名义，月度）。

用法：python3 scripts/study_guzhi_longrun.py  → 输出 logs/研究/估值版长窗口_<日期>.md 和同名 .csv
"""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "data" / "估值" / "美股长期"
OUT = ROOT / "logs" / "研究"
START, END = "1972-01-01", "2026-06-01"


def month(s):
    return pd.to_datetime(s).dt.to_period("M").dt.to_timestamp()


def load():
    sp = pd.read_csv(D / "标普500_席勒.csv")
    sp["m"] = month(sp["Date"])
    price = sp.set_index("m")["SP500"]
    dy = pd.read_csv(D / "标普股息率.csv"); dy["m"] = month(dy["date"]); dy = dy.groupby("m")["value"].last()
    gold = pd.read_csv(D / "黄金_月度.csv"); gold["m"] = month(gold["Date"]); gold = gold.set_index("m")["Price"]
    ty = pd.read_csv(D / "十年国债_multpl.csv"); ty["m"] = month(ty["date"]); ty = ty.groupby("m")["value"].last()
    cpi = pd.read_csv(D / "CPI_multpl.csv"); cpi["m"] = month(cpi["date"]); cpi = cpi.groupby("m")["value"].last()
    cape = pd.read_csv(ROOT / "data" / "估值" / "美股CAPE.csv"); cape["m"] = month(cape["date"]); cape = cape.groupby("m")["cape"].first()
    return price, dy, gold, ty, cpi, cape


def bond_ret(y0, y1):
    """上月收益率 y0 的十年平价债，持有一个月后按 y1 重新定价（剩余 119 个月）。"""
    c, r, n = y0 / 100 / 2, y1 / 100 / 2, 119 / 6
    k = np.arange(1, int(np.ceil(n)) + 1) - (np.ceil(n) - n)  # 半年付息时点
    pv = (c / (1 + r) ** k).sum() + 1 / (1 + r) ** k[-1]
    return pv - 1  # 价格变动；当月票息在调用处另加 y0/12


def main():
    price, dy, gold, ty, cpi, cape = load()
    idx = pd.date_range("1950-01-01", END, freq="MS")
    df = pd.DataFrame({"price": price, "dy": dy, "gold": gold, "ty": ty, "cpi": cpi}).reindex(idx)
    df["r_stock"] = df["price"] / df["price"].shift(1) - 1 + df["dy"].shift(1) / 100 / 12
    df["r_gold"] = df["gold"] / df["gold"].shift(1) - 1
    df["r_bond"] = [np.nan] + [bond_ret(a, b) + a / 100 / 12 for a, b in zip(df["ty"].values[:-1], df["ty"].values[1:])]
    df["infl"] = df["cpi"] / df["cpi"].shift(1) - 1

    # 估值分位：过去 240 个月（只用上月及以前）
    c = cape.reindex(pd.date_range("1871-01-01", END, freq="MS")).ffill()
    pct = c.rolling(240).apply(lambda x: (x <= x[-1]).mean() * 100, raw=True).shift(1)
    df["pct"] = pct.reindex(df.index)

    w = df.loc[START:END].copy()
    assert w[["r_stock", "r_gold", "r_bond", "infl", "pct"]].notna().all().all(), "数据有缺口"

    def tiers(p, step):
        return (0.6 - step, 0.2 + step) if p >= 80 else (0.6 + step, 0.2 - step) if p <= 20 else (0.6, 0.2)

    plans = {"A 固定股60债20金20": 0, "B 估值版±10": 0.10, "C 估值版±20": 0.20}
    navs, stock_w = {}, {}
    for name, step in plans.items():
        nav, hold, tgt, out, sw = 1.0, None, None, [], []
        for i, (d, r) in enumerate(w.iterrows()):
            if tgt is None:
                ws, wb = tiers(r["pct"], step) if step else (0.6, 0.2)
                tgt = {"s": ws, "b": wb, "g": 0.2}
                hold = dict(tgt)
            growth = {"s": 1 + r["r_stock"], "b": 1 + r["r_bond"], "g": 1 + r["r_gold"]}
            val = {k: hold[k] * growth[k] for k in hold}
            tot = sum(val.values()); nav *= tot
            hold = {k: v / tot for k, v in val.items()}
            out.append(nav); sw.append(tgt["s"])
            if d.month in (3, 6, 9, 12) and i + 1 < len(w):  # 季度末定档，下月生效
                p_next = w["pct"].iloc[i + 1]  # 下月可用的分位（已滞后一个月）
                ws, wb = tiers(p_next, step) if step else (0.6, 0.2)
                tgt = {"s": ws, "b": wb, "g": 0.2}
                hold = dict(tgt)
        navs[name] = pd.Series(out, index=w.index); stock_w[name] = pd.Series(sw, index=w.index)
    navs["D 全仓美股"] = (1 + w["r_stock"]).cumprod()
    cpi_idx = (1 + w["infl"]).cumprod()

    def stats(full, a, b):
        i0, i1 = w.index.get_loc(pd.Timestamp(a)), w.index.get_loc(pd.Timestamp(b))
        prev = 1.0 if i0 == 0 else float(full.iloc[i0 - 1])
        cprev = 1.0 if i0 == 0 else float(cpi_idx.iloc[i0 - 1])
        s = full.iloc[i0:i1 + 1]
        yrs = len(s) / 12
        nom = (s.iloc[-1] / prev) ** (1 / yrs) - 1
        real = ((s.iloc[-1] / prev) / (cpi_idx.iloc[i1] / cprev)) ** (1 / yrs) - 1
        path = pd.concat([pd.Series([prev]), s.reset_index(drop=True)])
        mdd = (path / path.cummax() - 1).min()
        return nom, real, mdd

    segs = [("全程 1972-01→2026-06", START, END), ("1972–1982（滞胀失落期）", "1972-01-01", "1982-12-01"),
            ("1983–1999（大牛市）", "1983-01-01", "1999-12-01"), ("2000–2012（失落十年）", "2000-01-01", "2012-12-01"),
            ("2013–2026-06（近十几年）", "2013-01-01", END)]
    rows = []
    for seg, a, b in segs:
        for name, s in navs.items():
            n, r, m = stats(s, a, b)
            rows.append({"区间": seg, "方案": name, "名义年化": n, "实际年化": r, "最大回撤": m})
    res = pd.DataFrame(rows)

    OUT.mkdir(parents=True, exist_ok=True)
    tag = date.today().isoformat()
    res.to_csv(OUT / f"估值版长窗口_{tag}.csv", index=False)
    lines = [f"# 参考研究：估值版长窗口（美股，{START[:7]} 至 {END[:7]}）", "",
             "不是固定口径回测：月度指数数据、不计费用、没有A股红利；只看估值档位在坏的十年里有没有用。规则见 scripts/study_guzhi_longrun.py 顶部，看结果前写定。", ""]
    for seg, _, _ in segs:
        lines += [f"## {seg}", "", "| 方案 | 名义年化 | 实际年化 | 最大回撤 |", "|---|---|---|---|"]
        for _, r in res[res["区间"] == seg].iterrows():
            lines.append(f"| {r['方案']} | {r['名义年化']:.2%} | {r['实际年化']:.2%} | {r['最大回撤']:.1%} |")
        lines.append("")
    sw = stock_w["B 估值版±10"]
    lines += ["## 估值档位分布（B 方案每月的股票目标仓位）", ""]
    for seg, a, b in segs:
        x = sw.loc[a:b]
        lines.append(f"- {seg}：贵档（股50%）{(x < 0.6).mean():.0%}，中档 {(x == 0.6).mean():.0%}，便宜档（股70%）{(x > 0.6).mean():.0%}")
    lines += ["", "数据：data/估值/美股长期/（席勒数据镜像、伦敦金、multpl 股息率/十年国债/CPI）与 data/估值/美股CAPE.csv。席勒价格为月均价，会把月内的急跌抹平一些，回撤偏浅。"]
    (OUT / f"估值版长窗口_{tag}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
