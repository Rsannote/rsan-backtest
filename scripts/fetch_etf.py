#!/usr/bin/env python3
"""抓取 ETF 日线并生成后复权数据（分红再投资，乘法复权），输出可直接给回测引擎用的 CSV。

数据来源：
- 腾讯 K 线接口：不复权日线（date, open, close, high, low, volume）
- 新浪 hfq.js：分红/拆分表（d=除息日，u=累计每份分红，s=拆分比例）

复权方法：从上市日起 F=1；除息日 F *= 前收/(前收-每份分红)；拆分日 F *= 拆分比例。
输出列：date, open, close, factor（open/close 为后复权价；factor=后复权价/真实价）。

规则：
- 输出目录按最后一个交易日命名 data/日线_<日期>；目录已存在就不覆盖（旧回测靠它复现）。
- 同时输出 data/日线_<日期>_分红核对.csv（不放在数据目录里，避免被引擎当成行情读取），
  列出每次分红/拆分当天的开盘跳空，用来核对除息日和金额对不对。
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, timedelta

import pandas as pd

CODES = ["510880", "513100", "511260", "518880"]
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}
UA_SINA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def get(url, headers, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败：{url} -> {last!r}")


def fetch_raw(code):
    sym = "sh" + code
    rows = {}
    start = date(2006, 1, 1)
    today = date.today() + timedelta(days=1)
    while start < today:
        end = min(date(start.year + 2, start.month, start.day) - timedelta(days=1), today)
        u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,"
             f"{start.isoformat()},{end.isoformat()},640,")
        j = json.loads(get(u, UA))
        node = (j.get("data") or {}).get(sym) or {}
        for r in node.get("day") or []:
            rows[r[0]] = (float(r[1]), float(r[2]))
        start = end + timedelta(days=1)
        time.sleep(0.3)
    if not rows:
        raise RuntimeError(f"{code}: 腾讯没有返回任何数据")
    df = pd.DataFrame(sorted((d, o, c) for d, (o, c) in rows.items()), columns=["date", "open", "close"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def drop_spikes(raw, code, removed):
    """剔除孤立坏点：当天收盘与前后两天都相差 40% 以上，而前后两天彼此相差不到 15%。
    （永久性的水平跳变，如拆分，前后两天差距很大，不会被误删。）"""
    prev_c, next_c = raw["close"].shift(1), raw["close"].shift(-1)
    spike = ((raw["close"] / prev_c - 1).abs() > 0.4) & ((raw["close"] / next_c - 1).abs() > 0.4) \
        & ((prev_c / next_c - 1).abs() < 0.15)
    for d in raw.index[spike.fillna(False)]:
        removed.append(f"{code} {d.date()} 孤立坏点已剔除：open={raw.at[d, 'open']} close={raw.at[d, 'close']}"
                       f"（前收 {prev_c.at[d]}，后收 {next_c.at[d]}）")
    return raw[~spike.fillna(False)]


def fetch_events(code):
    t = get(f"https://finance.sina.com.cn/realstock/company/sh{code}/hfq.js", UA_SINA)
    m = re.search(r"=\s*(\{.*?\})\s*(?:/\*|$)", t, re.S)
    if not m:
        raise RuntimeError(f"{code}: 新浪 hfq.js 格式解析失败：{t[:200]}")
    data = json.loads(m.group(1))["data"]
    ev = sorted((x for x in data if not x["d"].startswith("1900")), key=lambda x: x["d"])
    out, prev_u = [], 0.0
    for x in ev:
        u, s = float(x["u"]), float(x["s"])
        div = round(u - prev_u, 6)
        prev_u = u
        out.append({"ex_date": pd.Timestamp(x["d"]), "div": div if div > 0 else 0.0, "split": s})
    return out


def build(code, removed):
    raw = drop_spikes(fetch_raw(code), code, removed)
    events = fetch_events(code)
    cal = raw.index
    F = pd.Series(1.0, index=cal)
    checks = []
    f = 1.0
    ev_by_day = {}
    for e in events:
        pos = cal.searchsorted(e["ex_date"])  # 除息日不是交易日时，顺延到下一个交易日
        if pos >= len(cal) or pos == 0:
            continue
        ev_by_day.setdefault(cal[pos], []).append(e)
    for i, d in enumerate(cal):
        if d in ev_by_day:
            prev = raw["close"].iloc[i - 1]
            for e in ev_by_day[d]:
                if e["div"] > 0:
                    f *= prev / (prev - e["div"])
                if e["split"] not in (0.0, 1.0):
                    f *= e["split"]
                checks.append({"code": code, "ex_date": e["ex_date"].date(), "trade_date": d.date(),
                               "div": e["div"], "split": e["split"], "prev_close": prev,
                               "open": raw["open"].iloc[i], "close": raw["close"].iloc[i],
                               "open_gap": round(raw["open"].iloc[i] - prev, 4),
                               "close_gap": round(raw["close"].iloc[i] - prev, 4)})
        F.iloc[i] = f
    out = pd.DataFrame({"date": cal.strftime("%Y-%m-%d"),
                        "open": (raw["open"] * F).round(6).values,
                        "close": (raw["close"] * F).round(6).values,
                        "factor": F.round(8).values})
    return out, checks, raw


def main():
    frames, all_checks, warns, removed = {}, [], [], []
    for c in CODES:
        out, checks, raw = build(c, removed)
        frames[c] = out
        all_checks += checks
        # 后复权价的跳空：除拆分日外，不应出现大于 21% 的开盘跳空
        px = out["close"].shift(1)
        gap = (out["open"] / px - 1).abs()
        big = out[(gap > 0.21)]
        if len(big):
            warns.append(f"{c}: {len(big)} 个交易日后复权开盘跳空超过 21%：{list(big['date'])[:5]}")
        print(f"{c}: {out['date'].iloc[0]} → {out['date'].iloc[-1]}，{len(out)} 行，事件 {len(checks)} 个，最终复权系数 {out['factor'].iloc[-1]:.4f}")
    last = min(f["date"].iloc[-1] for f in frames.values())
    dd = f"data/日线_{last}"
    if os.path.exists(dd) and os.listdir(dd):
        print(f"{dd} 已存在，不覆盖（旧回测靠它复现）。")
        return 0
    os.makedirs(dd)
    for c, f in frames.items():
        f.to_csv(f"{dd}/{c}.csv", index=False)
    pd.DataFrame(all_checks).to_csv(f"{dd}_分红核对.csv", index=False, encoding="utf-8-sig")
    os.makedirs("logs", exist_ok=True)
    with open(f"logs/fetch_{last}.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(["【剔除的坏点】"] + (removed or ["无"]) + ["【跳空报警】"] + (warns or ["无"])))
    for w in warns:
        print("⚠️", w)
    print("输出目录：", dd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
