#!/usr/bin/env python3
"""探查新浪接口返回的真实格式（只存样本，不做任何计算）。结果写到 logs/explore/。"""
import json
import os
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
CODES = ["510880", "513100", "511260", "518880"]
OUT = "logs/explore"
os.makedirs(OUT, exist_ok=True)


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


for c in CODES:
    sym = "sh" + c
    info = {}
    try:
        t = get(f"https://finance.sina.com.cn/realstock/company/{sym}/hfq.js")
        info["hfq_len"] = len(t)
        info["hfq_head"] = t[:1500]
        info["hfq_tail"] = t[-600:]
    except Exception as e:
        info["hfq_error"] = repr(e)
    try:
        u = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{sym}=/CN_MarketDataService.getKLineData"
             f"?symbol={sym}&scale=240&ma=no&datalen=6000")
        t = get(u)
        info["kline_len"] = len(t)
        info["kline_head"] = t[:600]
        info["kline_tail"] = t[-400:]
    except Exception as e:
        info["kline_error"] = repr(e)
    with open(f"{OUT}/{c}.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print(c, {k: (v if k.endswith("len") or k.endswith("error") else "...") for k, v in info.items()})
