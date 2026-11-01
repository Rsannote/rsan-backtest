#!/usr/bin/env python3
"""T3 海外指数日线来源探测（只读）：纳指、标普500、日经225、韩国KOSPI、恒生指数、恒生科技。

在 GitHub Actions 上运行，逐个来源 × 指数抓 2010 年起的日线，记录：能否访问、历史起止、行数、
最新数据日期（当地日期）、耗时、两次请求是否一致；再把同一指数不同来源的收盘价互相对照。
结果写 logs/海外行情探测.txt 和 logs/test/T3/探测.json。不写 data/，不改行情库。只用标准库。

本地离线自检：python3 scripts/probe_overseas.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import re
import sys
import time
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BJ = timezone(timedelta(hours=8))
START = date(2010, 1, 1)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
MAX_BYTES = 16 * 1024 * 1024

INDICES = {  # 名称: {来源: [候选代码…]}
    "纳斯达克综合": {"tencent": ["us.IXIC"], "sina_us": [".IXIC"], "stooq": ["^ndq"], "yahoo": ["^IXIC"], "eastmoney": ["100.NDX"]},
    "标普500": {"tencent": ["us.INX"], "sina_us": [".INX"], "stooq": ["^spx"], "yahoo": ["^GSPC"], "eastmoney": ["100.SPX"]},
    "日经225": {"stooq": ["^nkx"], "yahoo": ["^N225"], "eastmoney": ["100.N225"]},
    "韩国KOSPI": {"stooq": ["^kospi"], "yahoo": ["^KS11"], "eastmoney": ["100.KS11"]},
    "恒生指数": {"tencent": ["hkHSI"], "sina_hk": ["HSI"], "stooq": ["^hsi"], "yahoo": ["^HSI"], "eastmoney": ["100.HSI"]},
    "恒生科技": {"tencent": ["hkHSTECH"], "sina_hk": ["HSTECH"], "yahoo": ["HSTECH.HK", "^HSTECH"], "eastmoney": ["124.HSTECH"]},
}


def get(url: str, headers=None, timeout=25) -> bytes:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("响应超过16MB")
    return body


def num(x) -> float:
    v = float(str(x).replace(",", ""))
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"非正有限数：{x}")
    return v


def clean(rows):
    """[(date, open, high, low, close)]；重复日期必须一致，否则报错；只保留 2010 年起。"""
    out = {}
    for d, o, h, l, c in rows:
        d = date.fromisoformat(d[:10])
        if d < START:
            continue
        rec = (num(o), num(h), num(l), num(c))
        if d in out and out[d] != rec:
            raise ValueError(f"重复日期数值冲突：{d}")
        out[d] = rec
    if not out:
        raise ValueError("2010年起没有数据")
    return sorted((d, *v) for d, v in out.items())


# ---------- 各来源解析（可离线测试） ----------

def parse_tencent(body: bytes, sym: str):
    j = json.loads(body)
    data = (j.get("data") or {}).get(sym) or {}
    rows = data.get("day") or data.get("qfqday") or []
    return [(r[0], r[1], r[3], r[4], r[2]) for r in rows]          # 日期, 开, 收, 高, 低


def parse_sina_jsonp(body: bytes):
    text = body.decode("utf-8", "replace")
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        raise ValueError("新浪返回不是预期的 JSONP 列表")
    rows = json.loads(m.group(1))
    key = "d" if rows and "d" in rows[0] else "date"
    k = {"d": ("d", "o", "h", "l", "c"), "date": ("date", "open", "high", "low", "close")}[key]
    return [tuple(r[x] for x in k) for r in rows]


def parse_stooq(body: bytes):
    text = body.decode("utf-8", "replace")
    if not text.startswith("Date,"):
        raise ValueError("Stooq 未返回CSV：" + text[:60].replace("\n", " "))
    return [(r["Date"], r["Open"], r["High"], r["Low"], r["Close"]) for r in csv.DictReader(io.StringIO(text))]


def parse_yahoo(body: bytes):
    res = json.loads(body)["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        vals = [q[k][i] for k in ("open", "high", "low", "close")]
        if any(v is None for v in vals):
            continue
        local = datetime.fromtimestamp(ts + off, tz=timezone.utc).date().isoformat()   # 交易所当地日期
        out.append((local, *vals))
    return out


def parse_eastmoney(body: bytes):
    data = json.loads(body).get("data") or {}
    rows = [k.split(",") for k in data.get("klines") or []]
    return [(r[0], r[1], r[3], r[4], r[2]) for r in rows]           # 日期, 开, 收, 高, 低


# ---------- 抓取 ----------

def fetch(source: str, sym: str, today: date):
    if source == "tencent":
        rows, s = [], START
        while s <= today:                                        # 分段取，避免单次条数上限
            e = min(date(s.year + 1, 12, 31), today)
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,{s},{e},640,"
            rows += parse_tencent(get(url), sym)
            s = e + timedelta(days=1)
        return rows
    if source == "sina_us":
        return parse_sina_jsonp(get(f"https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20t=/US_MinKService.getDailyK?symbol={sym}",
                                    {"Referer": "https://finance.sina.com.cn"}))
    if source == "sina_hk":
        return parse_sina_jsonp(get(f"https://quotes.sina.cn/hk/api/jsonp_v2.php/var%20t=/HK_StockService.getHKDayKLine?symbol={sym}",
                                    {"Referer": "https://finance.sina.com.cn"}))
    if source == "stooq":
        return parse_stooq(get("https://stooq.com/q/d/l/?s=" + urllib.request.quote(sym) + "&i=d"))
    if source == "yahoo":
        p1 = int(datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
        return parse_yahoo(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}?period1={p1}&period2={p2}&interval=1d"))
    if source == "eastmoney":
        return parse_eastmoney(get(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={sym}&fields1=f1,f2,f3,f4,f5,f6"
                                   f"&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=0&beg=20100101&end=20500101",
                                   {"Referer": "https://quote.eastmoney.com"}))
    raise ValueError("未知来源")


def attempt(source, sym, today):
    t0 = time.time()
    try:
        rows = clean(fetch(source, sym, today))
        return {"ok": True, "秒": round(time.time() - t0, 1), "行数": len(rows), "起": rows[0][0].isoformat(),
                "止": rows[-1][0].isoformat(), "最新收盘": rows[-1][4], "_rows": rows}
    except Exception as e:  # noqa: BLE001  探测要记录所有失败
        return {"ok": False, "秒": round(time.time() - t0, 1), "错误": f"{type(e).__name__}: {str(e)[:160]}"}


def compare(a, b, sample=5, seed=20261101):
    """同一指数两个来源：共同日期数、近250个共同日收盘最大相对差、随机5天对照。"""
    da = {r[0]: r[4] for r in a}
    db = {r[0]: r[4] for r in b}
    common = sorted(set(da) & set(db))
    if not common:
        return {"共同日期": 0}
    recent = common[-250:]
    diffs = [abs(da[d] / db[d] - 1) for d in recent]
    rnd = random.Random(seed).sample(common, min(sample, len(common)))
    return {"共同日期": len(common), "近250日最大相对差": round(max(diffs), 6),
            "近250日相差超0.1%天数": sum(x > 0.001 for x in diffs),
            "抽样": [(d.isoformat(), da[d], db[d]) for d in sorted(rnd)],
            "只在前者": len(set(da) - set(db)), "只在后者": len(set(db) - set(da))}


def run(now: datetime):
    today = now.date()
    results = {}
    for name, sources in INDICES.items():
        results[name] = {}
        for source, syms in sources.items():
            for sym in syms:
                first = attempt(source, sym, today)
                time.sleep(1.5)
                second = attempt(source, sym, today) if first["ok"] else None
                if second:
                    same = second["ok"] and second["行数"] == first["行数"] and second["止"] == first["止"]
                    first["两次一致"] = bool(same)
                    if not second["ok"]:
                        first["第二次错误"] = second["错误"]
                results[name][f"{source}:{sym}"] = first
                print(f"{name} {source}:{sym} → {'成功 ' + str(first['行数']) + '行 ' + first['起'] + '~' + first['止'] if first['ok'] else first['错误']}", flush=True)
                time.sleep(1.5)
                if first["ok"]:
                    break                                         # 同一来源的候选代码，首个成功即可
    for name, res in results.items():
        ok = [k for k, v in res.items() if v["ok"]]
        res["_对照"] = {f"{ok[i]} vs {ok[j]}": compare(res[ok[i]]["_rows"], res[ok[j]]["_rows"])
                        for i in range(len(ok)) for j in range(i + 1, len(ok))}
    return results


def report(results, now: datetime) -> str:
    lines = [f"# 海外指数日线来源探测（T3）", "", f"运行：{now.isoformat(timespec='seconds')}（北京时间），GitHub Actions；目标 2010-01-01 起日线。",
             "日期为各来源给出的交易所当地日期（雅虎按 gmtoffset 换算成当地日期）。每个来源请求两次，比较行数和最新日期是否一致。", ""]
    for name, res in results.items():
        lines += [f"## {name}", "", "|来源:代码|结果|行数|起|止（当地）|最新收盘|耗时秒|两次一致|", "|---|---|---:|---|---|---:|---:|---|"]
        for k, v in res.items():
            if k.startswith("_"):
                continue
            if v["ok"]:
                lines.append(f"|{k}|成功|{v['行数']}|{v['起']}|{v['止']}|{v['最新收盘']:g}|{v['秒']}|{v.get('两次一致', '—')}|")
            else:
                lines.append(f"|{k}|失败：{v['错误']}|—|—|—|—|{v['秒']}|—|")
        if res["_对照"]:
            lines += ["", "来源互相对照（收盘价）："]
            for pair, c in res["_对照"].items():
                if not c.get("共同日期"):
                    lines.append(f"- {pair}：没有共同日期")
                    continue
                lines.append(f"- {pair}：共同 {c['共同日期']} 天；近250日最大相对差 {c['近250日最大相对差']:.4%}，超0.1%的 {c['近250日相差超0.1%天数']} 天；"
                             f"日期只在前者 {c['只在前者']}、只在后者 {c['只在后者']}")
                lines.append("  抽样：" + "；".join(f"{d} {x:g} / {y:g}" for d, x, y in c["抽样"]))
        lines.append("")
    lines += ["来源取舍由审核决定；本探测不写 data/。", ""]
    return "\n".join(lines)


def strip(results):
    return {n: {k: {kk: vv for kk, vv in v.items() if kk != "_rows"} if not k.startswith("_") else v for k, v in r.items()}
            for n, r in results.items()}


# ---------- 离线自检 ----------

class SelfTest(unittest.TestCase):
    def test_parsers(self):
        t = json.dumps({"data": {"hkHSI": {"day": [["2010-01-04", "100", "110", "112", "99", "1"]]}}}).encode()
        self.assertEqual(clean(parse_tencent(t, "hkHSI"))[0][1:], (100.0, 112.0, 99.0, 110.0))
        s = b'var t=([{"d":"2010-01-04","o":"1","h":"3","l":"0.5","c":"2","v":"1"}]);'
        self.assertEqual(clean(parse_sina_jsonp(s))[0][4], 2.0)
        st = b"Date,Open,High,Low,Close,Volume\n2009-12-31,1,1,1,1,0\n2010-01-04,1,2,0.5,1.5,0\n"
        self.assertEqual(len(clean(parse_stooq(st))), 1)
        y = json.dumps({"chart": {"result": [{"meta": {"gmtoffset": 32400}, "timestamp": [1262563200],
                        "indicators": {"quote": [{"open": [1], "high": [2], "low": [0.5], "close": [1.5]}]}}]}}).encode()
        self.assertEqual(parse_yahoo(y)[0][0], "2010-01-04")
        e = json.dumps({"data": {"klines": ["2010-01-04,1,1.5,2,0.5,10"]}}).encode()
        self.assertEqual(clean(parse_eastmoney(e))[0][4], 1.5)

    def test_conflict_and_compare(self):
        with self.assertRaises(ValueError):
            clean([("2010-01-04", 1, 1, 1, 1), ("2010-01-04", 1, 1, 1, 2)])
        a = clean([("2010-01-04", 1, 1, 1, 100), ("2010-01-05", 1, 1, 1, 101)])
        b = clean([("2010-01-04", 1, 1, 1, 100), ("2010-01-05", 1, 1, 1, 101.5)])
        c = compare(a, b)
        self.assertEqual(c["共同日期"], 2)
        self.assertEqual(c["近250日相差超0.1%天数"], 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return 0 if unittest.main(argv=[sys.argv[0]], exit=False).result.wasSuccessful() else 1
    now = datetime.now(BJ)
    results = run(now)
    (ROOT / "logs/test/T3").mkdir(parents=True, exist_ok=True)
    (ROOT / "logs/海外行情探测.txt").write_text(report(results, now), encoding="utf-8")
    (ROOT / "logs/test/T3/探测.json").write_text(json.dumps(strip(results), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("已写 logs/海外行情探测.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
