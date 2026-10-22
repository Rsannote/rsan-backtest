#!/usr/bin/env python3
"""市场状态（云端v1）：每个交易日收盘后算指标、按 system/市场状态规则.md 给规则结论。

    python3 scripts/market_state.py            # 处理行情库里最新的交易日（已生成就跳过）
    python3 scripts/market_state.py --force    # 重算最新交易日
    python3 scripts/market_state.py --date 2026-10-15   # 用已有数据重算某天（不联网取实时数据）

由 .github/workflows/market-state.yml 在行情库更新完成后自动运行。规则参数在下面 RULES，
改动要升 VERSION 并同步改 system/市场状态规则.md。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

VERSION = "云端v1"
RULES = {
    "量比_高": 1.10, "量比_低": 0.85,          # 成交额 ÷ 前20日均
    "两融5日_高": 1.0, "两融5日_低": -1.0,      # 融资余额5日变化 %
    "涨跌停差_高": 30, "涨跌停差_低": 0,        # 涨停 − 跌停
    "炸板率_高": 40.0,                          # %
    "攻_总分": 3, "守_总分": -2,
    "热_分位": 90, "冷_分位": 10, "热_偏离": 6.0, "冷_偏离": -6.0,
    "热_连板": 7, "冷_跌停": 50, "热_涨停": 120, "冷_涨停": 20,
    "主线_天数": 3, "主线_窗口": 5, "前N": 5,
    "新启动_量倍": 1.5, "过热_5日": 15.0, "过热_偏离": 15.0, "过热_20日": 25.0, "拥挤_分位": 90,
    "热_炸板率": 35.0, "热_炸板率需成交分位": 60, "热_两融5日": 3.0,
}
# 旧版（Mac 股票助理 assistant.py）的进/守/空，只作对照，不参与新版结论
OLD = {"过热_成交额分位": 90, "过热_涨停家数": 100, "过热_沪深300偏离20日线": 6, "过热_炸板率": 35,
       "过热_两融5日增幅": 3, "偏冷_成交额低于20日均": 0.8, "偏冷_涨停家数": 30}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HQ = os.path.join(ROOT, "data", "行情库")
LIST = os.path.join(ROOT, "data", "标的清单.csv")
OUT = os.path.join(ROOT, "data", "市场状态")
IND = os.path.join(OUT, "日指标.csv")
MARGIN = os.path.join(OUT, "融资余额.csv")
BJ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Referer": "https://quote.eastmoney.com/"}
IND_COLS = ["date", "成交额亿", "涨停", "跌停", "炸板", "最高连板", "来源"]


def get_json(url, tries=3):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            err = e
            time.sleep(2 + 3 * i)
    raise err


# ---------------- 取实时数据 ----------------

def live_amount(date):
    """两市成交额（亿元）。东财为主、腾讯为备；数据时间必须是 date 当天。"""
    try:
        js = get_json("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001&fields=f12,f6,f124")
        rows = js["data"]["diff"]
        if all(dt.datetime.fromtimestamp(r["f124"], BJ).strftime("%Y-%m-%d") == date for r in rows):
            return round(sum(float(r["f6"]) for r in rows) / 1e8, 1), "东财"
    except Exception:
        pass
    try:
        req = urllib.request.Request("https://qt.gtimg.cn/q=sh000001,sz399001", headers=UA)
        txt = urllib.request.urlopen(req, timeout=20).read().decode("gbk", errors="replace")
        tot = 0.0
        for line in txt.split(";"):
            if "~" not in line:
                continue
            f = line.split('"')[1].split("~")
            if f[30][:8] != date.replace("-", ""):
                return None, "腾讯数据日期不是当天"
            tot += float(f[35].split("/")[2])
        return round(tot / 1e8, 1), "腾讯"
    except Exception as e:
        return None, f"成交额取不到：{type(e).__name__}"


def live_pools(date):
    ut, d = "7eea3edcaed734bea9cbfc24409ed989", date.replace("-", "")
    res = {}
    for key, api, sort in (("涨停", "getTopicZTPool", "fbt%3Aasc"), ("跌停", "getTopicDTPool", "fund%3Aasc"),
                           ("炸板", "getTopicZBPool", "fbt%3Aasc")):
        js = get_json(f"https://push2ex.eastmoney.com/{api}?ut={ut}&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort={sort}&date={d}")
        data = js.get("data")
        if data is None:  # 当天无数据（非交易日或尚未生成）
            res[key] = None
            continue
        if str(data.get("qdate")) != d:
            res[key] = None
            continue
        res[key] = int(data.get("tc") or 0)
        if key == "涨停":
            res["最高连板"] = max([int(p.get("lbc") or 1) for p in data.get("pool") or []] or [0])
    return res


def update_margin():
    """融资余额（亿元），合并进 融资余额.csv。"""
    js = get_json("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ&columns=DIM_DATE,RZYE"
                  "&source=WEB&sortColumns=DIM_DATE&sortTypes=-1&pageNumber=1&pageSize=60")
    new = pd.DataFrame([{"date": r["DIM_DATE"][:10], "融资余额亿": round(r["RZYE"] / 1e8, 1)} for r in js["result"]["data"]])
    old = pd.read_csv(MARGIN, dtype={"date": str}) if os.path.exists(MARGIN) else pd.DataFrame(columns=["date", "融资余额亿"])
    old = old[~old["date"].isin(new["date"])]
    out = pd.concat([old, new]).sort_values("date")
    out.to_csv(MARGIN, index=False)
    return out


# ---------------- 读数据 ----------------

def read_hq(kind, code):
    p = os.path.join(HQ, kind, f"{code}.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p, dtype={"date": str}).set_index("date")
    df["adj"] = df["close"] * df["factor"]
    return df


def sector_list():
    lst = pd.read_csv(LIST, dtype={"code": str})
    lst = lst[lst["group"] == "行业ETF"].copy()

    def kv(note, key):
        for part in str(note).split("；"):
            if part.startswith(key + "="):
                return part.split("=", 1)[1]
        return ""
    lst["风格"] = lst["note"].map(lambda n: kv(n, "风格"))
    lst["行业"] = lst["note"].map(lambda n: kv(n, "行业"))
    return lst


def read_ind():
    if os.path.exists(IND):
        return pd.read_csv(IND, dtype={"date": str})
    return pd.DataFrame(columns=IND_COLS)


# ---------------- 计算 ----------------

def sgn(x, hi, lo):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0
    return 1 if x > hi else (-1 if x < lo else 0)


def market_part(date, ind, hs, margin):
    R = RULES
    h = hs.loc[:date]
    c = h["close"]
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    close, m20, m60 = c.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    dev20 = (close / m20 - 1) * 100
    hsd = {"close": round(close, 2), "chg": round((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2),
           "chg5": round((c.iloc[-1] / c.iloc[-6] - 1) * 100, 2), "ma20": round(m20, 2), "ma60": round(m60, 2),
           "dev20": round(dev20, 2), "above20": bool(close > m20), "above60": bool(close > m60),
           "ma20向上": bool(m20 > ma20.iloc[-6])}

    ind = ind.set_index("date").sort_index()
    row = ind.loc[date] if date in ind.index else None
    amt = float(row["成交额亿"]) if row is not None and pd.notna(row["成交额亿"]) else None
    prev = ind.loc[:date]["成交额亿"].dropna()
    prev = prev[prev.index < date]
    avg20 = prev.iloc[-20:].mean() if len(prev) >= 15 else None
    avg5 = prev.iloc[-5:].mean() if len(prev) >= 5 else None
    hist = ind.loc[:date]["成交额亿"].dropna().iloc[-250:]
    pct = int(round((hist <= amt).mean() * 100)) if amt is not None and len(hist) >= 120 else None
    ratio = amt / avg20 if amt is not None and avg20 else None

    def iv(k):
        return int(row[k]) if row is not None and pd.notna(row.get(k)) else None
    zt, dtn, zb, lb = iv("涨停"), iv("跌停"), iv("炸板"), iv("最高连板")
    zbl = round(zb / (zt + zb) * 100, 1) if zt is not None and zb is not None and (zt + zb) > 0 else None

    m = margin[margin["date"] <= date].sort_values("date")
    mg = None
    if len(m) >= 6:
        mg = {"日期": m["date"].iloc[-1], "余额亿": float(m["融资余额亿"].iloc[-1]),
              "5日变化": round((m["融资余额亿"].iloc[-1] / m["融资余额亿"].iloc[-6] - 1) * 100, 2)}

    sig = [
        ("趋势", "沪深300与60日线", 1 if hsd["above60"] else -1),
        ("趋势", "沪深300与20日线", 1 if hsd["above20"] else -1),
        ("趋势", "20日线方向", 1 if hsd["ma20向上"] else -1),
        ("资金", "成交额÷前20日均", sgn(ratio, R["量比_高"], R["量比_低"]) if ratio else 0),
        ("资金", "融资余额5日变化", sgn(mg["5日变化"], R["两融5日_高"], R["两融5日_低"]) if mg else 0),
        ("情绪", "涨停−跌停", (1 if zt - dtn >= R["涨跌停差_高"] else -1 if zt - dtn <= R["涨跌停差_低"] else 0)
         if zt is not None and dtn is not None else 0),
        ("情绪", "炸板率", -1 if zbl is not None and zbl >= R["炸板率_高"] else 0),
    ]
    score = sum(s[2] for s in sig)
    if score <= R["守_总分"] or (not hsd["above20"] and not hsd["above60"]):
        concl = "守"
    elif score >= R["攻_总分"] and hsd["above60"]:
        concl = "攻"
    else:
        concl = "中性"

    hot, cold = [], []
    if pct is not None and pct >= R["热_分位"]:
        hot.append(f"成交额处在近一年 {pct}% 分位")
    if pct is not None and pct <= R["冷_分位"]:
        cold.append(f"成交额处在近一年 {pct}% 分位")
    if dev20 >= R["热_偏离"]:
        hot.append(f"沪深300 偏离20日线 +{dev20:.1f}%")
    if dev20 <= R["冷_偏离"]:
        cold.append(f"沪深300 偏离20日线 {dev20:.1f}%")
    if lb is not None and lb >= R["热_连板"]:
        hot.append(f"最高 {lb} 连板")
    if dtn is not None and dtn >= R["冷_跌停"]:
        cold.append(f"跌停 {dtn} 家")
    if zt is not None and zt >= R["热_涨停"]:
        hot.append(f"涨停 {zt} 家")
    if zt is not None and zt <= R["冷_涨停"]:
        cold.append(f"涨停仅 {zt} 家")
    if zbl is not None and zbl >= R["热_炸板率"] and (pct or 0) >= R["热_炸板率需成交分位"]:
        hot.append(f"炸板率 {zbl}%（放量下追涨开始亏钱）")
    if mg and mg["5日变化"] >= R["热_两融5日"]:
        hot.append(f"融资余额5日 +{mg['5日变化']}%（杠杆快速加仓）")
    if zt is not None and dtn is not None and dtn > zt:
        cold.append(f"跌停 {dtn} 多于涨停 {zt}")
    if hot and cold:
        temp = "正常（冷热分歧）"
    elif len(hot) >= 2:
        temp = "过热"
    elif hot:
        temp = "偏热"
    elif len(cold) >= 2:
        temp = "过冷"
    elif cold:
        temp = "偏冷"
    else:
        temp = "正常"

    old = old_verdict(amt, avg20, pct, zt, dtn, zbl, mg, hsd)
    basis = []
    if amt is not None:
        basis.append(f"两市成交 {amt:.0f} 亿（5日均 {avg5:.0f}，20日均 {avg20:.0f}，近一年分位 {pct}%）"
                     if avg5 and avg20 and pct is not None else f"两市成交 {amt:.0f} 亿")
    basis.append(f"沪深300 {hsd['chg']:+.2f}%，{'高于' if hsd['above20'] else '低于'}20日线（偏离 {dev20:+.2f}%），"
                 f"{'高于' if hsd['above60'] else '低于'}60日线，20日线{'向上' if hsd['ma20向上'] else '向下'}")
    if zt is not None:
        basis.append(f"涨停 {zt} / 跌停 {dtn} / 炸板 {zb}（炸板率 {zbl}%），最高 {lb} 连板")
    if mg:
        basis.append(f"融资余额 {mg['余额亿']:.0f} 亿（{mg['日期']}），5日变化 {mg['5日变化']:+.2f}%")
    return {"结论": concl, "总分": score, "冷热": temp, "热信号": hot, "冷信号": cold,
            "信号": [{"组": a, "信号": b, "分": s} for a, b, s in sig], "依据": basis,
            "沪深300": hsd, "成交额": amt, "成交额5日均": avg5 and round(avg5, 1), "成交额20日均": avg20 and round(avg20, 1),
            "成交额分位": pct, "量比": ratio and round(ratio, 3), "涨停": zt, "跌停": dtn, "炸板": zb,
            "炸板率": zbl, "最高连板": lb, "两融": mg, "旧版对照": old}


def old_verdict(amt, avg20, pct, zt, dtn, zbl, mg, hsd):
    """旧版 Mac 股票助理的判定，原样照搬（沪深300 用价格指数），只作对照。"""
    O = OLD
    hot, cold = 0, []
    hot += pct is not None and pct >= O["过热_成交额分位"]
    hot += zt is not None and zt >= O["过热_涨停家数"]
    hot += hsd["dev20"] >= O["过热_沪深300偏离20日线"]
    hot += zbl is not None and zbl >= O["过热_炸板率"] and (pct or 0) >= 60
    hot += mg is not None and mg["5日变化"] >= O["过热_两融5日增幅"]
    if amt and avg20 and amt < avg20 * O["偏冷_成交额低于20日均"]:
        cold.append(1)
    if zt is not None and dtn is not None and dtn > zt:
        cold.append(1)
    if zt is not None and zt < O["偏冷_涨停家数"]:
        cold.append(1)
    if hsd["dev20"] < 0 and not hsd["above60"]:
        cold.append(1)
    up = hsd["dev20"] > 0
    vol_ok = bool(amt and avg20 and amt >= avg20 * 0.95)
    v = "进" if up and vol_ok and not cold else ("空" if not up and len(cold) >= 2 else "守")
    heat = "过热" if hot >= 2 else "偏热" if hot == 1 else "偏冷" if len(cold) >= 2 else "正常"
    return {"结论": v, "冷热": heat}


def short(name):
    """“养殖ETF国泰” → “养殖”。"""
    return name.split("ETF")[0] or name


def sector_part(date):
    R = RULES
    lst = sector_list()
    closes, amts, meta = {}, {}, {}
    for _, it in lst.iterrows():
        df = read_hq("ETF", it["code"])
        if df is None or date not in df.index:
            continue
        df = df.loc[:date].iloc[-300:]
        nm = short(it["name"])
        closes[nm] = df["adj"]
        amts[nm] = df["volume"] * df["close"]
        meta[nm] = {"code": it["code"], "全称": it["name"], "风格": it["风格"], "行业": it["行业"]}
    if len(closes) < 8:
        return None, [f"行业ETF 数据不足（{len(closes)} 只有当天数据）"]
    C = pd.DataFrame(closes).sort_index().loc[:date]
    A = pd.DataFrame(amts).sort_index().loc[:date].fillna(0)
    ret = C.pct_change() * 100
    W = R["主线_窗口"]
    top = ret.iloc[-W:].rank(axis=1, ascending=False) <= R["前N"]
    ret5 = (C.iloc[-1] / C.iloc[-1 - W] - 1) * 100
    share = A.div(A.sum(axis=1), axis=0)
    s5 = A.iloc[-5:].sum() / A.iloc[-5:].sum().sum() * 100
    s20 = A.iloc[-25:-5].sum() / A.iloc[-25:-5].sum().sum() * 100
    flow = (s5 - s20).sort_values(ascending=False)
    roll5 = share.rolling(5).mean()
    crowd = {}
    for n in roll5.columns:
        h = roll5[n].dropna().iloc[-250:]
        if len(h) >= 120:
            crowd[n] = int(round((h <= h.iloc[-1]).mean() * 100))
    dev20 = (C.iloc[-1] / C.rolling(20).mean().iloc[-1] - 1) * 100
    today = ret.iloc[-1].sort_values(ascending=False)
    vol_mult = A.iloc[-1] / A.iloc[-21:-1].mean()

    main = [n for n in C.columns if top[n].sum() >= R["主线_天数"] and ret5[n] > 0]
    main.sort(key=lambda n: -ret5[n])
    ret20 = (C.iloc[-1] / C.iloc[-21] - 1) * 100
    new = [n for n in today.index[:R["前N"]] if vol_mult[n] >= R["新启动_量倍"] and not top[n].iloc[:-1].any()]
    hotb = [n for n in C.columns if ret5[n] >= R["过热_5日"] or dev20[n] >= R["过热_偏离"] or ret20[n] >= R["过热_20日"]]
    crowded = sorted([n for n, p in crowd.items() if p >= R["拥挤_分位"]], key=lambda n: -crowd[n])

    styles = {}
    for st in sorted({m["风格"] for m in meta.values() if m["风格"]}):
        ns = [n for n in C.columns if meta[n]["风格"] == st]
        styles[st] = {"5日涨跌": round(float(ret5[ns].mean()), 2), "成交占比变化": round(float(flow[ns].sum()), 2),
                      "数量": len(ns)}
    so = sorted(styles, key=lambda s: -styles[s]["成交占比变化"])
    ins = [s for s in so if styles[s]["成交占比变化"] > 0.5]
    outs = [s for s in reversed(so) if styles[s]["成交占比变化"] < -0.5]
    style_txt = ("资金在往" + "".join(f"「{s}」" for s in ins[:2]) + "流" if ins else "风格间没有明显的资金迁移") + \
                ("，从" + "".join(f"「{s}」" for s in outs[:2]) + "流出" if outs else "") + "（近5日成交占比 vs 之前20日）"

    detail = {n: {**meta[n], "今日": round(float(ret.iloc[-1][n]), 2), "5日": round(float(ret5[n]), 2), "20日": round(float(ret20[n]), 2),
                  "偏离20日线": round(float(dev20[n]), 2), "近5日进前5天数": int(top[n].sum()),
                  "成交占比变化": round(float(flow[n]), 2), "拥挤分位": crowd.get(n)} for n in C.columns}
    warn = []
    missing = [it["name"] for _, it in lst.iterrows() if short(it["name"]) not in closes]
    if missing:
        warn.append("这些行业ETF 当天无数据：" + "、".join(missing))
    return {"行业数": len(C.columns), "主线": main, "新启动": new, "过热板块": hotb, "拥挤": crowded,
            "今日涨幅前5": list(today.index[:5]), "今日跌幅前5": list(today.index[::-1][:5]),
            "资金流入前5": list(flow.index[:5]), "资金流出前5": list(flow.index[::-1][:5]),
            "风格": styles, "风格结论": style_txt, "明细": detail}, warn


def compose_text(date, mk, sec):
    L = [f"市场：{mk['结论']} · {mk['冷热']}（{date}，规则{VERSION}，总分 {mk['总分']:+d}；旧版对照：{mk['旧版对照']['结论']} · {mk['旧版对照']['冷热']}）"]
    L += [f"- {b}" for b in mk["依据"]]
    if mk["热信号"] or mk["冷信号"]:
        L.append("- 冷热信号：" + "；".join(mk["热信号"] + mk["冷信号"]))
    if sec:
        L.append(f"板块（{sec['行业数']} 个行业ETF近似）：{sec['风格结论']}")
        L.append(f"- 主线：{'、'.join(sec['主线']) or '无'}；新启动：{'、'.join(sec['新启动']) or '无'}")
        L.append(f"- 资金流入：{'、'.join(sec['资金流入前5'])}；流出：{'、'.join(sec['资金流出前5'])}")
        L.append(f"- 今日涨幅前5：{'、'.join(sec['今日涨幅前5'])}；跌幅前5：{'、'.join(sec['今日跌幅前5'])}")
        if sec["过热板块"] or sec["拥挤"]:
            L.append(f"- 过热：{'、'.join(sec['过热板块']) or '无'}；拥挤：{'、'.join(sec['拥挤']) or '无'}")
    return "\n".join(L)


def write_md(res):
    mk, sec = res["市场"], res["板块"]
    L = [f"# 市场状态 · {res['数据日期']}", "", f"规则 {VERSION}（`system/市场状态规则.md`）；生成于 {res['生成时间']}。"
         "这是程序按规则归纳的指标，不是判断，也不构成投资建议。", "",
         f"## 大盘：{mk['结论']} · {mk['冷热']}（总分 {mk['总分']:+d}）", "",
         f"旧版（Mac 股票助理）规则对照：{mk['旧版对照']['结论']} · {mk['旧版对照']['冷热']}", ""]
    L += [f"- {b}" for b in mk["依据"]]
    L += ["", "| 组 | 信号 | 分 |", "|---|---|---|"] + [f"| {s['组']} | {s['信号']} | {s['分']:+d} |" for s in mk["信号"]]
    if mk["热信号"] or mk["冷信号"]:
        L += ["", "冷热信号：" + "；".join(mk["热信号"] + mk["冷信号"])]
    if sec:
        L += ["", f"## 板块（{sec['行业数']} 个行业ETF近似）", "", sec["风格结论"], ""]
        for k in ("主线", "新启动", "资金流入前5", "资金流出前5", "今日涨幅前5", "今日跌幅前5", "过热板块", "拥挤"):
            L.append(f"- {k}：{'、'.join(sec[k]) or '无'}")
        L += ["", "| 风格 | 5日涨跌% | 成交占比变化(百分点) |", "|---|---|---|"]
        L += [f"| {s} | {v['5日涨跌']:+.2f} | {v['成交占比变化']:+.2f} |" for s, v in sec["风格"].items()]
        L += ["", "| 行业ETF | 对应行业 | 今日% | 5日% | 偏离20日线% | 占比变化 | 拥挤分位 |", "|---|---|---|---|---|---|---|"]
        for n, d in sorted(sec["明细"].items(), key=lambda x: -x[1]["5日"]):
            L.append(f"| {n} | {d['行业'] or '—'} | {d['今日']:+.2f} | {d['5日']:+.2f} | {d['偏离20日线']:+.2f} | "
                     f"{d['成交占比变化']:+.2f} | {d['拥挤分位'] if d['拥挤分位'] is not None else '—'} |")
    if res["数据告警"]:
        L += ["", "## 数据告警", ""] + [f"- {w}" for w in res["数据告警"]]
    with open(os.path.join(OUT, "最新.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--date", default="")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    hs = read_hq("指数", "sh000300")
    today = dt.datetime.now(BJ).strftime("%Y-%m-%d")
    date = a.date or hs.index[-1]
    if date not in hs.index:
        sys.exit(f"{date} 不是行情库里的交易日")
    day_path = os.path.join(OUT, date[:7], f"{date}.json")
    if os.path.exists(day_path) and not (a.force or a.date):
        print(f"{date} 已生成，跳过（要重算加 --force）")
        return
    warn = []
    ind = read_ind()

    # 实时数据：只在处理当天时取
    if not a.date:
        try:
            margin = update_margin()
        except Exception as e:
            warn.append(f"融资余额更新失败：{type(e).__name__}")
            margin = pd.read_csv(MARGIN, dtype={"date": str}) if os.path.exists(MARGIN) else pd.DataFrame(columns=["date", "融资余额亿"])
        have = ind[(ind["date"] == date) & (ind["来源"] == "实时")]
        if date == today and have.empty:
            amt, src = live_amount(date)
            if amt is None:
                warn.append(f"两市成交额未取到（{src}）")
            try:
                pools = live_pools(date)
            except Exception as e:
                pools = {}
                warn.append(f"涨跌停池未取到：{type(e).__name__}")
            for k in ("涨停", "跌停", "炸板"):
                if pools.get(k) is None:
                    warn.append(f"{k}数据未取到")
            row = {"date": date, "成交额亿": amt, "涨停": pools.get("涨停"), "跌停": pools.get("跌停"),
                   "炸板": pools.get("炸板"), "最高连板": pools.get("最高连板"), "来源": "实时"}
            ind = pd.concat([ind[ind["date"] != date], pd.DataFrame([row])]).sort_values("date")
            ind[IND_COLS].to_csv(IND, index=False)
        elif date != today and have.empty:
            warn.append(f"处理的是 {date}，不是今天，当天的成交额和涨跌停只能用已有记录")
    else:
        margin = pd.read_csv(MARGIN, dtype={"date": str}) if os.path.exists(MARGIN) else pd.DataFrame(columns=["date", "融资余额亿"])
    for c in ("成交额亿", "涨停", "跌停", "炸板", "最高连板"):
        ind[c] = pd.to_numeric(ind[c], errors="coerce")

    mk = market_part(date, ind, hs, margin)
    sec, w2 = sector_part(date)
    warn += w2
    if mk["两融"] and mk["两融"]["日期"] < (pd.Timestamp(date) - pd.Timedelta(days=6)).strftime("%Y-%m-%d"):
        warn.append(f"融资余额最新只到 {mk['两融']['日期']}")
    r = ind[ind["date"] == date]
    if not r.empty and r["来源"].iloc[0] == "回补估算":
        warn.append("当天成交额和涨跌停是回补估算值")
    res = {"版本": VERSION, "数据日期": date, "生成时间": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M"),
           "市场": mk, "板块": sec, "数据告警": warn}
    res["文字"] = compose_text(date, mk, sec)
    os.makedirs(os.path.dirname(day_path), exist_ok=True)
    js = json.dumps(res, ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    with open(day_path, "w", encoding="utf-8") as f:
        f.write(js + "\n")
    if not a.date or date == hs.index[-1]:
        with open(os.path.join(OUT, "最新.json"), "w", encoding="utf-8") as f:
            f.write(js + "\n")
        write_md(res)
    print(res["文字"])
    if warn:
        print("告警：" + "；".join(warn))


if __name__ == "__main__":
    main()
