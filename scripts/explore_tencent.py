#!/usr/bin/env python3
"""探查腾讯 K 线接口：不复权日线能取多少、格式如何、分页是否可行。结果写到 logs/explore/。"""
import json
import os
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}
OUT = "logs/explore"
os.makedirs(OUT, exist_ok=True)


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


res = {}
for sym in ["sh510880", "sh513100"]:
    info = {}
    # 1) 不带日期，取最近 5 根
    u1 = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,5,"
    t1 = get(u1)
    info["recent_raw"] = t1[:800]
    # 2) 指定区间分页：2017-01-01 到 2018-12-31
    u2 = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,2017-01-01,2018-12-31,640,"
    t2 = get(u2)
    info["range_len"] = len(t2)
    try:
        j = json.loads(t2)
        node = j["data"][sym]
        info["keys"] = list(node.keys())
        rows = node.get("day") or node.get("qfqday") or []
        info["n_rows"] = len(rows)
        info["first3"] = rows[:3]
        info["last3"] = rows[-3:]
    except Exception as e:
        info["parse_error"] = repr(e)
        info["range_head"] = t2[:500]
    res[sym] = info
with open(f"{OUT}/tencent.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print("done")
