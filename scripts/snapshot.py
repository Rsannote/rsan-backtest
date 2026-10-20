#!/usr/bin/env python3
"""从行情库切出一个固定的回测数据目录 data/日线_<截止日>/，格式直接给引擎 v1.1 用。

输出列：date,open,high,low,close,volume,factor
  open/high/low/close 为后复权价（= 原始价 × factor，分红再投资），factor = 后复权价/真实价，
  volume 为原始成交量（股票、ETF 单位是手）。
四个基准 ETF（510880、513100、511260、518880）总会包含在内。
目录一经用于回测就不再改动：目录已存在时，内容完全相同就直接复用，不同就自动换名（_2、_3…）。

用法：
  python3 scripts/snapshot.py --codes 600900,000333            # 基准 + 这两只
  python3 scripts/snapshot.py --groups 股票池,常用ETF          # 按标的清单的 group 列选
  python3 scripts/snapshot.py --all --end 2026-09-30           # 全部标的，截止到某天
  python3 scripts/snapshot.py --codes 600900 --out data/日线_长电专用
"""
import argparse
import csv
import filecmp
import os
import sys
import tempfile
import shutil

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "data", "行情库")
BENCH = ["510880", "513100", "511260", "518880"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--groups", default="", help="标的清单 group 列：基准、常用ETF、股票池、指数")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--end", help="截止日期 YYYY-MM-DD（含）；默认取四个基准共同的最后交易日")
    ap.add_argument("--out", help="输出目录；默认 data/日线_<截止日>")
    a = ap.parse_args()

    items = {x["code"]: x for x in csv.DictReader(open(os.path.join(ROOT, "data", "标的清单.csv"), encoding="utf-8"))}
    want = list(BENCH)
    if a.all:
        want += list(items)
    if a.groups:
        gs = set(a.groups.split(","))
        want += [c for c, x in items.items() if x["group"] in gs]
    if a.codes:
        want += [c.strip() for c in a.codes.split(",") if c.strip()]
    want = list(dict.fromkeys(want))
    miss = [c for c in want if c not in items]
    if miss:
        sys.exit(f"不在标的清单里：{miss}。先加进 data/标的清单.csv 并运行 update_hangqing.py --codes ...")

    frames = {}
    for c in want:
        p = os.path.join(LIB, items[c]["type"], f"{c}.csv")
        if not os.path.exists(p):
            sys.exit(f"行情库里还没有 {c}（{items[c]['name']}），先运行 update_hangqing.py --codes {c}")
        frames[c] = pd.read_csv(p, parse_dates=["date"]).set_index("date")

    end = pd.Timestamp(a.end) if a.end else min(frames[c].index[-1] for c in BENCH)
    tag = end.strftime("%Y-%m-%d")

    # 引擎按文件名里最后 6 位数字识别代码，指数 sh000001 和股票 000001 会撞车
    six = {}
    for c in want:
        k = "".join(ch for ch in c if ch.isdigit())[-6:]
        if k in six:
            sys.exit(f"{c} 和 {six[k]} 在引擎里都会被识别成 {k}，不能放进同一个目录")
        six[k] = c

    tmp = tempfile.mkdtemp(prefix="snap_", dir=os.path.join(ROOT, "data"))
    try:
        for c, df in frames.items():
            df = df.loc[:end]
            if df.empty:
                print(f"⚠️ {c} 在 {tag} 之前没有数据，跳过")
                continue
            f = df["factor"]
            out = pd.DataFrame({"open": (df["open"] * f).round(6), "high": (df["high"] * f).round(6),
                                "low": (df["low"] * f).round(6), "close": (df["close"] * f).round(6),
                                "volume": df["volume"], "factor": f.round(8)})
            out.index = out.index.strftime("%Y-%m-%d")
            out.index.name = "date"
            out.to_csv(os.path.join(tmp, f"{c}.csv"), float_format="%.10g")

        base = a.out or os.path.join("data", f"日线_{tag}")
        base = os.path.join(ROOT, base) if not os.path.isabs(base) else base
        cand, n = base, 1
        while os.path.exists(cand) and os.listdir(cand):
            cmp = filecmp.dircmp(cand, tmp)
            if not (cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files):
                print(f"{os.path.relpath(cand, ROOT)} 已存在且内容完全相同，直接用它。")
                return 0
            n += 1
            cand = f"{base}_{n}"
        os.makedirs(os.path.dirname(cand), exist_ok=True)
        if os.path.exists(cand):
            os.rmdir(cand)
        shutil.move(tmp, cand)
        tmp = None
        print(f"已生成 {os.path.relpath(cand, ROOT)}：{len(os.listdir(cand))} 个标的，截止 {tag}")
        if cand != base:
            print(f"（{os.path.relpath(base, ROOT)} 已被别的数据占用，所以换了名字）")
    finally:
        if tmp and os.path.exists(tmp):
            shutil.rmtree(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
