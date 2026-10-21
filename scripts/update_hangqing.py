#!/usr/bin/env python3
"""更新行情库：按 data/标的清单.csv 抓取不复权日线和分红/复权表，算出后复权系数，存进 data/行情库/。

行情库是“活的”总库，每个交易日自动追加；回测不要直接读它，而是用 scripts/snapshot.py
从行情库切出一个固定的 data/日线_<日期>/ 目录再回测（那个目录一经使用就不再改动）。

存储（全部可离线重算）：
  data/行情库/<ETF|股票|指数>/<代码>.csv   列 date,open,high,low,close,volume,factor
      open/high/low/close/volume 是**不复权**原始值（腾讯 K 线），factor 是后复权系数（后复权价 = 原始价 × factor）。
  data/行情库/分红表/<代码>.json           新浪 hfq.js 的原始事件表
  data/行情库/状态.csv                      每个标的的起止日期、行数、最近分红、文件 sha256

复权方法（乘法复权，分红再投资）：
  股票：新浪 hfq.js 给出每个除权日的累计后复权因子 f，当天及以后用该 f；
  ETF ：新浪 hfq.js 给出累计每份分红 u、拆分比例 s，除息日 F *= 前收/(前收-每份分红)，拆分日 F *= s
        （与 scripts/fetch_etf.py 相同，2026-10-21 已核对与电脑数据一致）；
  指数：只有价格指数，factor=1。

用法：
  python3 scripts/update_hangqing.py                 # 增量：每个标的只重抓最近约 40 天，并和已存数据核对
  python3 scripts/update_hangqing.py --full          # 全部全量重抓
  python3 scripts/update_hangqing.py --full --rotate # 约四分之一全量重抓、其余增量（每周六自动跑，四周轮一遍，核对历史有没有被改）
  python3 scripts/update_hangqing.py --codes 600900,510880   # 只更新这几个（必须已在标的清单里）
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(ROOT, "data", "标的清单.csv")
LIB = os.path.join(ROOT, "data", "行情库")
EVD = os.path.join(LIB, "分红表")
UA_QQ = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}
UA_SINA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
COLS = ["open", "high", "low", "close", "volume"]
FULL_START = date(2005, 1, 1)
OVERLAP_DAYS = 40
BJ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 网络
_lock = threading.Lock()
_next = [0.0]
PACE = 0.8  # 两次请求之间至少间隔的秒数（腾讯请求太密会返回 501）


def get(url, headers, tries=5):
    last = None
    for i in range(tries):
        with _lock:  # 全局节流
            wait = _next[0] - time.time()
            if wait > 0:
                time.sleep(wait)
            _next[0] = time.time() + PACE
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429, 501, 503):  # 被限流：所有线程一起暂停
                with _lock:
                    _next[0] = time.time() + 30 * (i + 1)
            else:
                time.sleep(2 * (i + 1))
        except Exception as e:  # 超时、断连
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"请求失败：{url} -> {last!r}")


def symbol(code, typ):
    if typ == "指数":
        return code  # 清单里直接写 sh000300 / sz399006
    return ("sh" if code[0] in "569" else "sz") + code


def fetch_raw(sym, start):
    """腾讯不复权日线，start 到今天，按 3 年一段请求。"""
    rows = {}
    end_all = date.today() + timedelta(days=1)
    s = start
    while s < end_all:
        e = min(date(s.year + 3, s.month, 1) - timedelta(days=1), end_all)
        u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,"
             f"{s.isoformat()},{e.isoformat()},900,")
        j = json.loads(get(u, UA_QQ))
        node = (j.get("data") or {}).get(sym) or {}
        for r in node.get("day") or []:
            if len(r) >= 6:
                rows[r[0]] = [float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5])]
        s = e + timedelta(days=1)
    # 腾讯日线顺序是 日期,开,收,高,低,量 → 这里存成 开,高,低,收,量
    df = pd.DataFrame([[d] + v for d, v in sorted(rows.items())], columns=["date"] + COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def fetch_events(sym):
    t = get(f"https://finance.sina.com.cn/realstock/company/{sym}/hfq.js", UA_SINA)
    m = re.search(r"=\s*(\{.*?\})\s*(?:/\*|$)", t, re.S)
    if not m:
        raise RuntimeError(f"{sym}: 新浪 hfq.js 格式解析失败：{t[:120]!r}")
    data = json.loads(m.group(1)).get("data") or []
    return sorted((x for x in data if not str(x.get("d", "")).startswith("1900")), key=lambda x: x["d"])


# ---------------------------------------------------------------- 计算
def drop_spikes(raw, code, notes):
    """孤立坏点：当天收盘与前后两天都相差 40% 以上，而前后两天彼此相差不到 15%。"""
    pc, nc = raw["close"].shift(1), raw["close"].shift(-1)
    spike = (((raw["close"] / pc - 1).abs() > 0.4) & ((raw["close"] / nc - 1).abs() > 0.4)
             & ((pc / nc - 1).abs() < 0.15)).fillna(False)
    for d in raw.index[spike]:
        notes.append(f"{code} {d.date()} 孤立坏点已剔除：open={raw.at[d, 'open']} close={raw.at[d, 'close']}（前收 {pc.at[d]}，后收 {nc.at[d]}）")
    return raw[~spike]


def factors(raw, events, typ):
    cal = raw.index
    F = pd.Series(1.0, index=cal)
    if typ == "指数" or not events:
        return F
    if "u" not in events[0]:  # 股票：只有累计因子 f（ETF 的表里也有 f，但恒为 1，要用 u/s）
        ev = pd.Series([float(x["f"]) for x in events], index=pd.to_datetime([x["d"] for x in events]))
        ev = ev[~ev.index.duplicated(keep="last")].sort_index()
        pos = ev.index.searchsorted(cal, side="right") - 1
        vals = [ev.iloc[p] if p >= 0 else 1.0 for p in pos]
        # 上市前就有的事件（因子 >1 但早于本库第一天）不会出现；上市当天之前一律按 1
        return pd.Series(vals, index=cal, dtype=float)
    # ETF：u = 累计每份分红，s = 累计份额折算比例（两者都是累计值，相邻事件相减/相除得到当次）。
    # 折算的生效日：新浪有时把登记日和实际折算日都列出来，所以在事件日起 4 个交易日内，
    # 选开盘/前收最接近 1/当次比例 的那天生效。
    by_day, prev_u, prev_s = {}, 0.0, 1.0
    opens, closes = raw["open"].values, raw["close"].values
    for x in events:
        u, s = float(x.get("u", 0) or 0), float(x.get("s", 1) or 1)
        div = round(u - prev_u, 6)
        ratio = s / prev_s if s > 0 else 1.0
        prev_u, prev_s = u, (s if s > 0 else prev_s)
        p = cal.searchsorted(pd.Timestamp(x["d"]))  # 除息日不是交易日时顺延
        if not 0 < p < len(cal):
            continue
        if div > 0:
            by_day.setdefault(p, []).append(("div", div))
        if abs(ratio - 1) > 0.01:
            cands = range(p, min(p + 4, len(cal)))
            best = min(cands, key=lambda k: abs(np.log(opens[k] / closes[k - 1]) + np.log(ratio)))
            by_day.setdefault(best, []).append(("split", ratio))
    f = 1.0
    out = []
    for i in range(len(cal)):
        for kind, v in by_day.get(i, []):
            if kind == "div":
                f *= closes[i - 1] / (closes[i - 1] - v)
            else:
                f *= v
        out.append(f)
    return pd.Series(out, index=cal)


def read_store(path):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df


def write_store(path, df):
    out = df.copy()
    for c in ["open", "high", "low", "close"]:
        out[c] = out[c].round(4)
    out["volume"] = out["volume"].round(2)
    out["factor"] = out["factor"].round(10)
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    out[COLS + ["factor"]].to_csv(path, float_format="%.10g")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# ---------------------------------------------------------------- 单个标的
def update_one(item, full):
    code, typ, name = item["code"], item["type"], item["name"]
    sym = symbol(code, typ)
    path = os.path.join(LIB, typ, f"{code}.csv")
    evp = os.path.join(EVD, f"{code}.json")
    rep = {"code": code, "name": name, "type": typ, "notes": [], "status": "ok"}
    old = None if full else read_store(path)
    try:
        if old is not None and len(old):
            start = (old.index[-1] - pd.Timedelta(days=OVERLAP_DAYS)).date()
            tail = fetch_raw(sym, start)
            ov = old.index.intersection(tail.index)
            mism = [d for d in ov if any(abs(old.at[d, c] - tail.at[d, c]) > 1e-6 * max(1, abs(tail.at[d, c]))
                                         for c in ["open", "close"])]
            if len(tail) == 0:
                raise RuntimeError("腾讯没有返回最近的数据")
            if mism or len(ov) == 0:
                rep["notes"].append(f"最近数据与已存不一致（{len(mism)} 天，如 {mism[:3]}），改为全量重抓")
                raw = fetch_raw(sym, FULL_START)
            else:
                raw = pd.concat([old.loc[old.index < tail.index[0], COLS], tail[COLS]])
        else:
            raw = fetch_raw(sym, FULL_START)
        if len(raw) == 0:
            raise RuntimeError("腾讯没有返回任何数据")
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()
        raw = raw[(raw["open"] > 0) & (raw["close"] > 0)]
        raw = drop_spikes(raw, code, rep["notes"])

        events = []
        if typ != "指数":
            old_ev = json.load(open(evp, encoding="utf-8")) if os.path.exists(evp) else None
            try:
                events = fetch_events(sym)
            except Exception as e:
                if old_ev is None:
                    raise
                events = old_ev
                rep["notes"].append(f"新浪分红表获取失败，沿用已存的分红表：{e}")
            if old_ev is not None:
                new = [x for x in events if x not in old_ev]
                gone = [x for x in old_ev if x not in events]
                if new:
                    rep["notes"].append(f"新的分红/除权事件：{[x['d'] for x in new]}")
                if gone:
                    rep["notes"].append(f"⚠️ 分红表里有旧事件被修改或删除：{[x['d'] for x in gone]}")
        raw["factor"] = factors(raw, events, typ)

        # 自检：后复权开盘相对前一天后复权收盘跳空超过 21%
        adj_o, adj_c = raw["open"] * raw["factor"], raw["close"] * raw["factor"]
        gap = (adj_o / adj_c.shift(1) - 1).abs()
        big = gap[gap > 0.21]
        if len(big):
            rep["gaps"] = [str(d.date()) for d in big.index]

        # 和旧文件比：历史行有没有被改动（不只是追加）
        prev = read_store(path)
        if prev is not None:
            common = prev.index.intersection(raw.index)
            chg = [d for d in common
                   if abs(prev.at[d, "close"] * prev.at[d, "factor"] - raw.at[d, "close"] * raw.at[d, "factor"])
                   > 1e-6 * raw.at[d, "close"] * raw.at[d, "factor"]]
            lost = prev.index.difference(raw.index)
            if chg:
                rep["notes"].append(f"⚠️ {len(chg)} 个历史交易日的后复权价发生变化（首个 {chg[0].date()}）")
            if len(lost):
                rep["notes"].append(f"⚠️ {len(lost)} 个历史交易日被删除（如 {[str(d.date()) for d in lost[:3]]}）")
            if len(raw) < 0.9 * len(prev):
                raise RuntimeError(f"新数据只有 {len(raw)} 行，比已存的 {len(prev)} 行少太多，不覆盖")
            rep["added"] = int(len(raw.index.difference(prev.index)))
        else:
            rep["added"] = len(raw)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_store(path, raw)
        if typ != "指数":
            os.makedirs(EVD, exist_ok=True)
            with open(evp, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=0)
        rep.update(first=str(raw.index[0].date()), last=str(raw.index[-1].date()), rows=len(raw),
                   events=len(events), last_event=(events[-1]["d"] if events else ""),
                   factor_last=round(float(raw["factor"].iloc[-1]), 6), sha256=sha(path)[:16])
    except Exception as e:
        rep["status"] = "失败"
        rep["notes"].append(f"{type(e).__name__}: {e}"[:300])
    return rep


# ---------------------------------------------------------------- 主程序
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量重抓")
    ap.add_argument("--codes", default="", help="只更新这些代码，逗号分隔")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--budget", type=float, default=60, help="最多用多少分钟；超时后不再开始新的标的（没轮到的下次自动补上）")
    ap.add_argument("--rotate", action="store_true", help="配合 --full：只全量重抓约四分之一的标的（按周轮换，四周一轮），其余增量")
    a = ap.parse_args()

    items = list(csv.DictReader(open(LIST, encoding="utf-8")))
    if a.codes:
        want = set(a.codes.split(","))
        miss = want - {x["code"] for x in items}
        if miss:
            sys.exit(f"这些代码不在 data/标的清单.csv 里，请先加进清单：{sorted(miss)}")
        items = [x for x in items if x["code"] in want]

    t0 = time.time()
    # 先做还没有文件的（新标的），再按上次更新时间从旧到新，保证超时没轮到的下次优先
    stp0 = os.path.join(LIB, "状态.csv")
    last_upd = {}
    if os.path.exists(stp0):
        last_upd = {r["code"]: r.get("updated", "") for r in csv.DictReader(open(stp0, encoding="utf-8"))}
    items.sort(key=lambda it: last_upd.get(it["code"], ""))
    week = date.today().isocalendar()[1]
    def is_full(it):
        if not a.full:
            return False
        if not a.rotate:
            return True
        return int(hashlib.md5(it["code"].encode()).hexdigest(), 16) % 4 == week % 4
    with ThreadPoolExecutor(a.workers) as ex:
        def job(it):
            if time.time() - t0 > a.budget * 60:
                return {"code": it["code"], "name": it["name"], "type": it["type"], "status": "未轮到",
                        "notes": [f"超过 {a.budget:.0f} 分钟时间预算，下次运行补上"], "full": False}
            return update_one(it, is_full(it)) | {"full": is_full(it)}
        reps = list(ex.map(job, items))

    # 状态表：保留没在本次更新范围里的旧行
    stp = os.path.join(LIB, "状态.csv")
    keep = {}
    if os.path.exists(stp):
        for r in csv.DictReader(open(stp, encoding="utf-8")):
            keep[r["code"]] = r
    now = datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    fields = ["code", "name", "type", "first", "last", "rows", "events", "last_event", "factor_last", "sha256", "updated"]
    for r in reps:
        if r["status"] == "ok":
            keep[r["code"]] = {k: r.get(k, "") for k in fields} | {"updated": now}
    order = [x["code"] for x in csv.DictReader(open(LIST, encoding="utf-8"))]
    with open(stp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in order:
            if c in keep:
                w.writerow({k: keep[c].get(k, "") for k in fields})

    ok = [r for r in reps if r["status"] == "ok"]
    bad = [r for r in reps if r["status"] == "失败"]
    skipped = [r for r in reps if r["status"] == "未轮到"]
    lasts = sorted({r["last"] for r in ok})
    lines = [f"# 行情库更新 {now}（北京时间）", "",
             f"- 模式：全量重抓 {sum(r['full'] for r in reps)} 个，其余增量；标的 {len(reps)} 个，成功 {len(ok)}，失败 {len(bad)}，超时未轮到 {len(skipped)}；用时 {time.time() - t0:.0f} 秒",
             f"- 最新交易日：{lasts[-1] if lasts else '无'}" + (f"（有 {sum(r['last'] != lasts[-1] for r in ok)} 个标的最后日期更早，可能停牌）" if len(lasts) > 1 else ""),
             f"- 新增行数合计：{sum(r.get('added', 0) for r in ok)}", ""]
    if bad:
        lines += ["## 失败", ""] + [f"- {r['code']} {r['name']}：{'；'.join(r['notes'])}" for r in bad] + [""]
    if skipped:
        lines += [f"## 超时未轮到（下次运行优先补上）", "", "- " + "、".join(f"{r['code']} {r['name']}" for r in skipped), ""]
    noted = [r for r in ok if r["notes"]]
    if noted:
        lines += ["## 需要留意", ""] + [f"- {r['code']} {r['name']}：{'；'.join(r['notes'])}" for r in noted] + [""]
    gapped = [r for r in ok if r.get("gaps")]
    if gapped:
        lines += ["## 后复权跳空超过 21% 的交易日（多为长期停牌复牌或上市首日，复权有误时也会出现）", ""]
        lines += [f"- {r['code']} {r['name']}：{len(r['gaps'])} 天，{r['gaps'][:6]}" for r in gapped] + [""]
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    text = "\n".join(lines)
    with open(os.path.join(ROOT, "logs", "行情库_最新.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 1 if len(bad) > max(2, 0.1 * len(reps)) else 0


if __name__ == "__main__":
    sys.exit(main())
