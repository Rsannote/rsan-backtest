#!/usr/bin/env python3
"""散户情绪指标（规则 v1，见 system/情绪规则.md）：情绪温度计 + 文本指标 + 触发判断。

在 GitHub 服务器上由 .github/workflows/sentiment-index.yml 在「市场状态」跑完后自动运行，也可以手动：
    python3 scripts/sentiment_index.py              # 处理最新一天
    python3 scripts/sentiment_index.py --date 2026-10-22

产出（data/社区情绪/）：
- 温度计.csv     每个交易日的温度和三个分项分位（每次从原始数据整体重算，口径与回测案例78相同）
- 文本指标.csv   每天一行的文本指标（按日期更新同一行）
- 最新.md        最新一天的温度、文本指标、触发情况，给人和 Claude 快速看
- 触发.json      最新一天是否触发、原因，给定时任务“散户情绪·触发判读”读
这些都是程序按规则算出的指标，不是判断。
"""
import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

VERSION = "v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MS = os.path.join(ROOT, "data", "市场状态")
OUT = os.path.join(ROOT, "data", "社区情绪")
WIN = 750
MIN_HIST = 20          # 文本指标历史不足这么多天，只记录、不触发
ZONES = [(20, "冰点"), (40, "偏冷"), (60, "中性"), (80, "偏热"), (101, "过热")]


def zone(t):
    for hi, name in ZONES:
        if t < hi:
            return name
    return "过热"


# ---------- 温度计 ----------

def _pct_rank(s):
    return s.rolling(WIN, min_periods=WIN).apply(lambda x: (x <= x[-1]).mean() * 100, raw=True)


def temperature():
    d = pd.read_csv(os.path.join(MS, "日指标.csv"), parse_dates=["date"]).drop_duplicates("date", keep="last")
    d = d.set_index("date").sort_index()
    m = pd.read_csv(os.path.join(MS, "融资余额.csv"), parse_dates=["date"]).drop_duplicates("date", keep="last")
    m = m.set_index("date").sort_index()
    rz = m["融资余额亿"].reindex(d.index, method="ffill").shift(1)   # 次日公布，滞后一天
    f1 = _pct_rank(d["成交额亿"].rolling(20).mean())
    f2 = _pct_rank(rz.pct_change(60))
    f3 = _pct_rank((d["涨停"] - d["跌停"]).rolling(20).mean())
    t = pd.DataFrame({"成交额分位": f1, "杠杆分位": f2, "赚钱效应分位": f3})
    t["温度"] = t[["成交额分位", "杠杆分位", "赚钱效应分位"]].mean(axis=1, skipna=False)
    return t.dropna().round(1)[["温度", "成交额分位", "杠杆分位", "赚钱效应分位"]]


# ---------- 文本指标 ----------

def hit(text, words):
    return any(w in text for w in words)


def text_metrics(day_dir, lex):
    row = {}
    raw = os.path.join(day_dir, "讨论原始.jsonl")
    replies = []
    if os.path.exists(raw):
        with open(raw, encoding="utf-8") as f:
            for line in f:
                p = json.loads(line)
                replies += [r.get("内容") or "" for r in p.get("回复", []) if r.get("内容")]
    n = len(replies)
    row["回复数"] = n
    for k in ["恐慌", "贪婪", "抄底", "求助"]:
        c = sum(1 for r in replies if hit(r, lex[k]))
        row[f"{k}词数"] = c
        row[f"{k}词率"] = round(c / n * 100, 1) if n else np.nan
    row["情绪差"] = round(row["恐慌词率"] - row["贪婪词率"], 1) if n else np.nan
    gb = os.path.join(day_dir, "股吧上证吧帖子.csv")
    if os.path.exists(gb):
        g = pd.read_csv(gb)
        row["股吧阅读合计"] = int(pd.to_numeric(g.get("阅读"), errors="coerce").fillna(0).sum()) if "阅读" in g else np.nan
        row["股吧评论合计"] = int(pd.to_numeric(g.get("评论"), errors="coerce").fillna(0).sum()) if "评论" in g else np.nan
    wb = os.path.join(day_dir, "微博热搜.csv")
    if os.path.exists(wb):
        w = pd.read_csv(wb)
        topics = [t for t in w["话题"].astype(str) if hit(t, lex["财经出圈"])]
        row["微博出圈"] = len(topics)
        row["微博出圈话题"] = "；".join(topics)
    return row


def pct_in_hist(series, value):
    s = series.dropna()
    if len(s) < MIN_HIST or pd.isna(value):
        return None
    return round(float((s <= value).mean() * 100), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args()
    lex = json.load(open(os.path.join(ROOT, "system", "情绪词典.json"), encoding="utf-8"))

    # 温度计
    t = temperature()
    t.to_csv(os.path.join(OUT, "温度计.csv"), index_label="date", encoding="utf-8-sig")

    # 当天
    days = sorted(os.path.basename(p) for p in glob.glob(os.path.join(OUT, "20??-??-??")) if os.path.isdir(p))
    day = args.date or (days[-1] if days else t.index[-1].strftime("%Y-%m-%d"))
    tday = t.loc[:pd.Timestamp(day)]
    cur = tday.iloc[-1]
    prev = tday.iloc[-2] if len(tday) > 1 else None
    tdate = tday.index[-1].strftime("%Y-%m-%d")

    # 文本指标（按日期更新同一行）
    tm_path = os.path.join(OUT, "文本指标.csv")
    tm = pd.read_csv(tm_path, dtype={"date": str}) if os.path.exists(tm_path) else pd.DataFrame(columns=["date"])
    day_dir = os.path.join(OUT, day)
    row = {"date": day, "规则": VERSION, **text_metrics(day_dir, lex)} if os.path.isdir(day_dir) else None
    if row:
        tm = pd.concat([tm[tm["date"] != day], pd.DataFrame([row])], ignore_index=True).sort_values("date")
        tm.to_csv(tm_path, index=False, encoding="utf-8-sig")
    hist = tm[tm["date"] <= day]

    # 触发
    reasons = []
    temp = float(cur["温度"])
    if prev is not None:
        p = float(prev["温度"])
        for lvl, name in [(20, "冰点区"), (80, "过热区")]:
            if lvl == 20 and (p >= 20) and temp < 20:
                reasons.append(f"温度进入{name}（{p}→{temp}）")
            if lvl == 20 and (p < 20) and temp >= 20:
                reasons.append(f"温度离开{name}（{p}→{temp}）")
            if lvl == 80 and (p < 80) and temp >= 80:
                reasons.append(f"温度进入{name}（{p}→{temp}）")
            if lvl == 80 and (p >= 80) and temp < 80:
                reasons.append(f"温度离开{name}（{p}→{temp}）")
        if abs(temp - p) >= 15:
            reasons.append(f"温度单日变化 {temp - p:+.1f}")
    pcts = {}
    if row:
        if (row.get("微博出圈") or 0) >= 2:
            reasons.append(f"财经话题上微博热搜 {row['微博出圈']} 条：{row['微博出圈话题']}")
        for k in ["恐慌词率", "贪婪词率", "股吧阅读合计"]:
            pcts[k] = pct_in_hist(hist[k].astype(float) if k in hist else pd.Series(dtype=float), row.get(k))
        if pcts.get("恐慌词率") is not None and pcts["恐慌词率"] >= 95:
            reasons.append(f"恐慌词率 {row['恐慌词率']}% 处在历史 {pcts['恐慌词率']} 分位")
        if pcts.get("贪婪词率") is not None and pcts["贪婪词率"] >= 95:
            reasons.append(f"贪婪词率 {row['贪婪词率']}% 处在历史 {pcts['贪婪词率']} 分位")
        if pcts.get("股吧阅读合计") is not None and pcts["股吧阅读合计"] <= 5:
            reasons.append(f"股吧讨论量处在历史 {pcts['股吧阅读合计']} 分位（冷清候选）")

    trig = {"date": day, "温度日期": tdate, "温度": temp, "区间": zone(temp),
            "triggered": bool(reasons), "reasons": reasons, "规则": VERSION}
    json.dump(trig, open(os.path.join(OUT, "触发.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 最新.md
    t5 = tday["温度"].iloc[-6] if len(tday) > 5 else np.nan
    t20 = tday["温度"].iloc[-21] if len(tday) > 20 else np.nan
    lines = [f"# 散户情绪 · {day}", "",
             f"规则 {VERSION}（`system/情绪规则.md`）。程序按规则算的指标，不是判断，也不构成投资建议。", "",
             f"## 情绪温度计：{temp:.0f}（{zone(temp)}）", "",
             f"温度日期 {tdate}；5 日前 {t5:.0f}，20 日前 {t20:.0f}。"
             f"分项分位：成交额 {cur['成交额分位']:.0f}，杠杆（融资60日变化）{cur['杠杆分位']:.0f}，赚钱效应（涨停−跌停）{cur['赚钱效应分位']:.0f}。", "",
             "近 10 个交易日：" + " → ".join(f"{v:.0f}" for v in tday["温度"].iloc[-10:]), ""]
    if row:
        lines += ["## 文本指标", "",
                  f"历史 {len(hist)} 天（满 {MIN_HIST} 天后才算分位、才参与触发）。", "",
                  "| 指标 | 今天 | 历史分位 |", "|---|---|---|"]
        for k in ["回复数", "恐慌词率", "贪婪词率", "抄底词率", "求助词率", "情绪差", "股吧阅读合计", "股吧评论合计", "微博出圈"]:
            v = row.get(k)
            unit = "%" if k.endswith("率") else ""
            pc = pct_in_hist(hist[k].astype(float), v) if k in hist else None
            lines.append(f"| {k} | {'' if v is None or (isinstance(v, float) and np.isnan(v)) else v}{unit} | {'—' if pc is None else pc} |")
        if row.get("微博出圈话题"):
            lines += ["", f"微博财经话题：{row['微博出圈话题']}"]
    else:
        lines += ["## 文本指标", "", f"当天没有社区数据目录（{day}）。"]
    lines += ["", "## 触发", "", ("；".join(reasons) if reasons else "今天没有触发。"), ""]
    open(os.path.join(OUT, "最新.md"), "w", encoding="utf-8").write("\n".join(lines))
    print(json.dumps(trig, ensure_ascii=False))


if __name__ == "__main__":
    main()
