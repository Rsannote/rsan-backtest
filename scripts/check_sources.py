#!/usr/bin/env python3
"""检测行情数据源在当前运行环境下能不能访问。

只做连通性检测，不抓取、不保存行情数据。结果打印出来并写到 source_check.json。
用法：python3 scripts/check_sources.py
"""
import json
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; rsan-backtest source check)"}

SOURCES = {
    "新浪_ETF不复权日线": "https://finance.sina.com.cn/realstock/company/sh510880/hfq.js",
    "新浪_行情接口": "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_sh510880=/CN_MarketDataService.getKLineData?symbol=sh510880&scale=240&ma=no&datalen=5",
    "东财_K线接口": "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.510880&fields1=f1&fields2=f51,f52,f53&klt=101&fqt=0&end=20500101&lmt=5",
    "腾讯_K线接口": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh510880,day,,,5,hfq",
    "GitHub_开源数据集": "https://github.com/chenditc/investment_data/releases",
}


def check(url: str) -> dict:
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(300)
            return {"ok": True, "status": r.status, "bytes_sample": len(body),
                    "seconds": round(time.time() - t0, 2)}
    except Exception as e:  # 连不上、被拦截、超时都算失败
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200],
                "seconds": round(time.time() - t0, 2)}


def main():
    result = {name: check(url) for name, url in SOURCES.items()}
    for name, r in result.items():
        print(("通 " if r["ok"] else "不通 "), name, r)
    with open("source_check.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return 0 if any(r["ok"] for r in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
