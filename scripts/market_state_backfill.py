#!/usr/bin/env python3
"""市场状态的历史回补（一次性，或数据有缺口时再跑）。在 GitHub 服务器上运行：

    python3 scripts/market_state_backfill.py [--since 2014-01-01]

1. 下载 qlib 开源数据集（chenditc/investment_data），按每只 A 股的日线推算每天的：
   全市场成交额、涨停、跌停、炸板、最高连板（推算规则见下），写 data/市场状态/回补_qlib.csv（原始单位）。
2. 用上交所官网的每日成交金额校准 qlib 成交额的单位（qlib 沪市合计 ÷ 上交所官方），系数写进
   data/市场状态/回补_校准.json；不稳定（各天系数相差超过 3%）就不写日指标，只报告。
3. 把校准后的数据并进 data/市场状态/日指标.csv，来源记 `回补估算`；已有 `实时` 记录的日期不动。
4. 东财两融全部历史写 data/市场状态/融资余额.csv。

涨跌停推算：涨跌幅限制主板 10%，创业板（2020-08-24 起）和科创板 20%，北交所不计；
收盘价=最高价 且 涨幅 ≥ 限制−0.5 个百分点 记涨停；收盘价=最低价 且 跌幅 ≥ 限制−0.5 记跌停；
最高价达到涨停幅度但收盘没封住记炸板；上市不满 5 个交易日的不计。ST 股 5% 限制没有区分，会少算。
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "市场状态")
WORK = os.path.join(ROOT, "_qlib_tmp")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0", "Referer": "https://www.sse.com.cn/"}


def load_bin(path, n):
    b = open(path, "rb").read()
    arr = np.frombuffer(b, dtype="<f4")
    start = int(arr[0])
    out = np.full(n, np.nan, dtype="f8")
    vals = arr[1:].astype("f8")
    end = min(n, start + len(vals))
    out[start:end] = vals[: end - start]
    return out


def qlib_daily(since):
    os.makedirs(WORK, exist_ok=True)
    tgz = os.path.join(WORK, "q.tar.gz")
    subprocess.run(["curl", "-sSL", "-m", "1200", "-o", tgz,
                    "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"], check=True)
    subprocess.run(["tar", "-xzf", tgz, "-C", WORK, "--wildcards", "qlib_bin/calendars/*",
                    "*/amount.day.bin", "*/close.day.bin", "*/high.day.bin", "*/low.day.bin"], check=True)
    os.remove(tgz)
    cal = open(os.path.join(WORK, "qlib_bin/calendars/day.txt")).read().split()
    n = len(cal)
    i0 = next(i for i, d in enumerate(cal) if d >= since)
    fdir = os.path.join(WORK, "qlib_bin/features")
    calv = np.array(cal)
    tot = np.zeros(n)
    tot_sh = np.zeros(n)
    zt = np.zeros(n, int)
    dtn = np.zeros(n, int)
    zb = np.zeros(n, int)
    maxlb = np.zeros(n, int)
    for inst in sorted(os.listdir(fdir)):
        if not (inst.startswith("sh6") or inst.startswith("sz0") or inst.startswith("sz3")):
            continue
        d = os.path.join(fdir, inst)
        try:
            amt = load_bin(os.path.join(d, "amount.day.bin"), n)
            c = load_bin(os.path.join(d, "close.day.bin"), n)
            h = load_bin(os.path.join(d, "high.day.bin"), n)
            lo = load_bin(os.path.join(d, "low.day.bin"), n)
        except FileNotFoundError:
            continue
        a = np.nan_to_num(amt)
        tot += a
        if inst.startswith("sh"):
            tot_sh += a
        traded = ~np.isnan(c) & (a > 0)
        idx = np.where(traded)[0]
        if len(idx) < 6:
            continue
        # 前收：上一个有交易的日子
        prev = np.full(n, np.nan)
        prev[idx[1:]] = c[idx[:-1]]
        age = np.zeros(n, int)
        age[idx] = np.arange(len(idx))
        code = inst[2:]
        lim = np.full(n, 0.10)
        if code.startswith("688"):
            lim[:] = 0.20
        elif code.startswith("30"):
            lim[calv >= "2020-08-24"] = 0.20
        with np.errstate(invalid="ignore", divide="ignore"):
            chg = c / prev - 1
            hchg = h / prev - 1
            ok = traded & (age >= 5) & ~np.isnan(prev)
            up = ok & (chg >= lim - 0.005) & (np.abs(c - h) <= 1e-6 * np.abs(h) + 1e-9)
            down = ok & (chg <= -(lim - 0.005)) & (np.abs(c - lo) <= 1e-6 * np.abs(lo) + 1e-9)
            broke = ok & (hchg >= lim - 0.005) & ~up
        zt += up
        dtn += down
        zb += broke
        # 连板：连续涨停天数（按有交易的日子连续）
        run = 0
        for i in idx:
            run = run + 1 if up[i] else 0
            if run > maxlb[i]:
                maxlb[i] = run
    shutil.rmtree(WORK, ignore_errors=True)
    df = pd.DataFrame({"date": cal, "qlib成交额": tot, "qlib沪市成交额": tot_sh, "涨停": zt, "跌停": dtn,
                       "炸板": zb, "最高连板": maxlb}).iloc[i0:]
    return df[df["qlib成交额"] > 0]


def sse_amount(date):
    """上交所当日股票成交金额（亿元）。返回 None 表示取不到。"""
    url = ("https://query.sse.com.cn/commonQuery.do?jsonCallBack=&sqlId=COMMON_SSE_SJ_GPSJ_CJGK_DAYCJGK_C"
           f"&PRODUCT_CODE=01%2C02%2C03%2C11%2C17&type=inParams&SEARCH_DATE={date}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
        js = json.loads(r.read().decode("utf-8", errors="replace"))
    return js


def margin_history():
    rows = []
    for p in range(1, 15):
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ&columns=DIM_DATE,RZYE"
               f"&source=WEB&sortColumns=DIM_DATE&sortTypes=-1&pageNumber={p}&pageSize=500")
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            js = json.loads(r.read().decode("utf-8"))
        data = (js.get("result") or {}).get("data") or []
        rows += [{"date": d["DIM_DATE"][:10], "融资余额亿": round(d["RZYE"] / 1e8, 1)} for d in data]
        if p >= (js.get("result") or {}).get("pages", 0):
            break
        time.sleep(0.5)
    return pd.DataFrame(rows).drop_duplicates("date").sort_values("date")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2014-01-01")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    report = {}

    try:
        m = margin_history()
        m.to_csv(os.path.join(OUT, "融资余额.csv"), index=False)
        report["融资余额"] = f"{len(m)} 天，{m['date'].iloc[0]} 至 {m['date'].iloc[-1]}"
    except Exception as e:
        report["融资余额"] = f"失败 {type(e).__name__}: {e}"[:200]

    q = qlib_daily(a.since)
    q.to_csv(os.path.join(OUT, "回补_qlib.csv"), index=False)
    report["qlib"] = f"{len(q)} 天，{q['date'].iloc[0]} 至 {q['date'].iloc[-1]}"

    # 校准：取 qlib 最近 8 个交易日中的几天对照上交所
    samples = {}
    raw = {}
    for d in list(q["date"].iloc[-8:]) + list(q["date"].iloc[-260:-250]):
        try:
            js = sse_amount(d)
            raw[d] = js.get("result")
            res = js.get("result") or []
            # 产品 01=主板A、03=科创板（以返回里的 PRODUCT_NAME 为准，记录原文以便核对）
            amt = 0.0
            used = []
            for it in res:
                name = str(it.get("PRODUCT_NAME", ""))
                if any(k in name for k in ("主板A", "科创板")) or it.get("PRODUCT_CODE") in ("01", "03"):
                    v = it.get("TRADE_AMT")
                    if v not in (None, "", "-"):
                        amt += float(v)
                        used.append(name or it.get("PRODUCT_CODE"))
            if amt > 0:
                ql = float(q.loc[q["date"] == d, "qlib沪市成交额"].iloc[0])
                samples[d] = {"上交所": amt, "qlib沪市": ql, "系数": amt / ql, "产品": used}
        except Exception as e:
            samples[d] = {"错误": f"{type(e).__name__}: {e}"[:120]}
        time.sleep(0.4)
    with open(os.path.join(ROOT, "logs", "市场状态_回补校准原文.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    ks = [v["系数"] for v in samples.values() if "系数" in v]
    calib = {"样本": samples}
    ok = len(ks) >= 4 and (max(ks) / min(ks) - 1) < 0.03
    if ks:
        calib["系数_中位数"] = float(np.median(ks))
    calib["可用"] = bool(ok)
    with open(os.path.join(OUT, "回补_校准.json"), "w", encoding="utf-8") as f:
        json.dump(calib, f, ensure_ascii=False, indent=1)
    report["校准"] = f"{len(ks)} 个样本，系数 {calib.get('系数_中位数')}，{'可用' if ok else '不可用，未写日指标'}"

    if ok:
        k = calib["系数_中位数"]  # 上交所金额单位为亿元
        ind_p = os.path.join(OUT, "日指标.csv")
        ind = pd.read_csv(ind_p, dtype={"date": str}) if os.path.exists(ind_p) else pd.DataFrame(
            columns=["date", "成交额亿", "涨停", "跌停", "炸板", "最高连板", "来源"])
        live = set(ind.loc[ind["来源"] == "实时", "date"])
        bf = q[~q["date"].isin(live)].copy()
        bf["成交额亿"] = (bf["qlib成交额"] * k).round(1)
        bf["来源"] = "回补估算"
        cols = ["date", "成交额亿", "涨停", "跌停", "炸板", "最高连板", "来源"]
        out = pd.concat([ind[ind["来源"] == "实时"][cols], bf[cols]]).sort_values("date")
        out.to_csv(ind_p, index=False)
        report["日指标"] = f"写入 {len(bf)} 天回补，保留实时 {len(live)} 天"
    with open(os.path.join(ROOT, "logs", "市场状态_回补.md"), "w", encoding="utf-8") as f:
        f.write("# 市场状态历史回补\n\n" + "\n".join(f"- {k}：{v}" for k, v in report.items()) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps(samples, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    sys.exit(main())
