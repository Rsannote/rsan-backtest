#!/usr/bin/env python3
"""命脉股票池页面数据库 → 仓库镜像。

股票池数据在 claude.ai 页面数据库里，GitHub 服务器读不到，所以由 Claude 定时任务用
ArtifactData list（out_dir）把各集合导出到一个临时目录，再运行本脚本写进仓库：

    python3 scripts/pool_mirror.py --src <导出目录> [--date YYYY-MM-DD]

导出目录结构：<src>/<集合>/<文档id>.json（ArtifactData out_dir 的默认结构）。

产出（都在 data/股票池/ 下）：
- 原始/<集合>/<id>.json   与页面数据库逐条一致（键排序、两空格缩进）。每天覆盖为最新，历史靠 git 记录。
- 股票.csv               一只一行的平铺表，给程序用（第一层状态、类型、市值、计划状态等）。
- 镜像日志.jsonl          只追加：每次有变化时记一行，写清哪些文档新增/改动/删除。
- 状态.json               最近一次检查时间、各集合文档数。

保护：某个集合导出为空、或文档数比上次少 20% 以上，视为导出失败，整次不写入并以非零退出。
确实是大批删除时，加 --allow-shrink。
"""
import argparse
import csv
import datetime as dt
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "股票池")
RAW = os.path.join(OUT, "原始")
COLLECTIONS = ["meta", "stocks", "plans", "trades", "changes", "daily"]
# 允许为空的集合（交易日志可能还没记过）
MAY_BE_EMPTY = {"trades", "daily"}


def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def load_dir(d):
    docs = {}
    if not os.path.isdir(d):
        return docs
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                docs[fn[:-5]] = json.load(f)
    return docs


def g(d, *path, default=""):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return "" if d is None else d


def flat_table(stocks, plans):
    cols = ["code", "name", "sector", "nodeName", "type", "l1_status", "effType", "bucket",
            "mcap_亿", "amt_亿", "roe", "listDate", "fails", "q", "g", "v", "t",
            "lastReview", "plan_mode", "plan_status", "plan_rank", "plan_price", "plan_priceDate"]
    rows = []
    for code in sorted(stocks):
        s, p = stocks[code], plans.get(code, {})
        sc = s.get("scores") or {}
        rows.append({
            "code": code, "name": g(s, "name"), "sector": g(s, "sector"), "nodeName": g(s, "nodeName"),
            "type": g(s, "type"), "l1_status": g(s, "l1", "status"), "effType": g(s, "l1", "effType"),
            "bucket": g(s, "l1", "bucket"), "mcap_亿": g(s, "l1", "mcap"), "amt_亿": g(s, "l1", "amt"),
            "roe": g(s, "l1", "roe"), "listDate": g(s, "l1", "listDate"),
            "fails": "；".join(map(str, g(s, "l1", "fails", default=[]) or [])),
            "q": sc.get("quality", ""), "g": sc.get("growth", ""), "v": sc.get("value", ""), "t": sc.get("trend", ""),
            "lastReview": g(s, "lastReview"), "plan_mode": g(p, "mode"), "plan_status": g(p, "status"),
            "plan_rank": g(p, "rank"), "plan_price": g(p, "price"), "plan_priceDate": g(p, "priceDate"),
        })
    return cols, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--date", default=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d"))
    ap.add_argument("--allow-shrink", action="store_true")
    a = ap.parse_args()

    new = {c: load_dir(os.path.join(a.src, c)) for c in COLLECTIONS}
    old = {c: load_dir(os.path.join(RAW, c)) for c in COLLECTIONS}

    # 保护检查
    problems = []
    for c in COLLECTIONS:
        n_new, n_old = len(new[c]), len(old[c])
        if n_new == 0 and c not in MAY_BE_EMPTY:
            problems.append(f"{c} 导出为空")
        elif n_old and n_new < n_old * 0.8:
            problems.append(f"{c} 从 {n_old} 条降到 {n_new} 条")
    if problems and not a.allow_shrink:
        print("镜像中止（疑似导出不完整，未写入任何文件）：" + "；".join(problems))
        sys.exit(2)

    diff = {}
    for c in COLLECTIONS:
        added = sorted(set(new[c]) - set(old[c]))
        removed = sorted(set(old[c]) - set(new[c]))
        changed = sorted(k for k in set(new[c]) & set(old[c]) if canon(new[c][k]) != canon(old[c][k]))
        if added or removed or changed:
            diff[c] = {k: v for k, v in (("新增", added), ("改动", changed), ("删除", removed)) if v}

    # 写原始镜像（整目录替换，保证删掉的文档也删掉）
    tmp = RAW + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    for c in COLLECTIONS:
        os.makedirs(os.path.join(tmp, c), exist_ok=True)
        for k, v in new[c].items():
            with open(os.path.join(tmp, c, k + ".json"), "w", encoding="utf-8") as f:
                f.write(canon(v))
    shutil.rmtree(RAW, ignore_errors=True)
    os.replace(tmp, RAW)

    cols, rows = flat_table(new["stocks"], new["plans"])
    with open(os.path.join(OUT, "股票.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    counts = {c: len(new[c]) for c in COLLECTIONS}
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    with open(os.path.join(OUT, "状态.json"), "w", encoding="utf-8") as f:
        f.write(canon({"最近检查": now, "数据日期": a.date, "文档数": counts,
                       "来源": "https://claude.ai/artifact/Cqkv9RLHDb5Z1jz66zgjXb",
                       "最近一次有变化": a.date if diff else _last_change()}))
    if diff:
        with open(os.path.join(OUT, "镜像日志.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": a.date, "time": now, "counts": counts, "diff": diff},
                               ensure_ascii=False, sort_keys=True) + "\n")
        parts = [f"{c}：" + "，".join(f"{k}{len(v)}" for k, v in d.items()) for c, d in diff.items()]
        print("有变化 —— " + "；".join(parts))
    else:
        print("无变化")
    print("文档数：" + "，".join(f"{c} {n}" for c, n in counts.items()))


def _last_change():
    p = os.path.join(OUT, "镜像日志.jsonl")
    if not os.path.exists(p):
        return ""
    last = ""
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = json.loads(line).get("date", last)
    return last


if __name__ == "__main__":
    main()
