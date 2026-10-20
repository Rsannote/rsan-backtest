#!/usr/bin/env python3
"""探测个股和指数的数据格式（只看格式，不保存行情）。结果写到 logs/explore/stock_probe.txt。

看三件事：
1. 新浪 hfq.js 对个股返回什么字段（ETF 是 d/u/s，个股可能是 d/f）。
2. 腾讯 K 线不复权与 hfq 两种返回，以及含送转/配股的个股的样子。
3. 腾讯指数代码（sh000300、sh000922）能否取到日线。
"""
import json
import os
import time
import urllib.request

UA_QQ = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}
UA_SINA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
out = []


def get(url, h):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            err = e
            time.sleep(2)
    return f"<<失败 {err!r}>>"


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    out.append(s)


# 1. 新浪 hfq.js：长江电力（常规分红）、美的（深市）、中芯（科创板）、紫金（送转多）、中国平安
for sym in ["sh600900", "sz000333", "sh688981", "sh601899", "sh601318", "sz300308"]:
    t = get(f"https://finance.sina.com.cn/realstock/company/{sym}/hfq.js", UA_SINA)
    log(f"\n==== 新浪 hfq.js {sym} 长度 {len(t)}")
    log(t[:600])
    log("... 结尾：", t[-300:])
    t2 = get(f"https://finance.sina.com.cn/realstock/company/{sym}/qfq.js", UA_SINA)
    log(f"---- qfq.js {sym} 长度 {len(t2)}：", t2[:300])

# 2. 腾讯不复权 vs hfq，取紫金矿业 2008-2010（有送转）
for fq in ["", "hfq", "qfq"]:
    u = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh601899,day,2010-06-01,2010-07-31,640,{fq}"
    t = get(u, UA_QQ)
    log(f"\n==== 腾讯 sh601899 fq={fq or '不复权'} 长度 {len(t)}")
    try:
        j = json.loads(t)
        node = j["data"]["sh601899"]
        for k, v in node.items():
            if isinstance(v, list):
                log(k, len(v), v[:3], "...", v[-2:])
            else:
                log(k, str(v)[:200])
    except Exception as e:
        log("解析失败", e, t[:300])

# 3. 腾讯指数
for sym in ["sh000300", "sh000922", "sz399986", "sh000015"]:
    u = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,2026-09-01,2026-10-25,640,"
    t = get(u, UA_QQ)
    log(f"\n==== 腾讯指数 {sym}：", t[:400])
    u = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,2005-01-01,2006-12-31,640,"
    t = get(u, UA_QQ)
    log(f"---- {sym} 2005-2006 前三条：", t[:300])

# 4. 腾讯单次最多返回多少条（640 是上限吗）
u = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600900,day,2015-01-01,2020-12-31,2000,"
t = get(u, UA_QQ)
try:
    n = len(json.loads(t)["data"]["sh600900"]["day"])
except Exception as e:
    n = f"解析失败 {e}"
log("\n==== 腾讯一次请求 6 年、要 2000 条，实际返回：", n)

os.makedirs("logs/explore", exist_ok=True)
with open("logs/explore/stock_probe.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
