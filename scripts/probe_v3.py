#!/usr/bin/env python3
"""探测业绩加速（v3）选股需要的数据源在 GitHub 服务器上能否连通，结果写到 logs/explore/v3_probe.txt。
用法：jobs/run.txt 写 probe_v3.py 推送。"""
import os, sys, json, time, subprocess, traceback, urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "explore", "v3_probe.txt")
L = []
def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); L.append(s)

try:
    import akshare as ak
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "akshare"], check=False)
    import akshare as ak
log("akshare", ak.__version__)

def probe(name, fn):
    t = time.time()
    try:
        v = fn()
        log(f"\n==== {name} OK {time.time()-t:.1f}s")
        log(str(v)[:1500])
    except Exception as e:
        log(f"\n==== {name} 失败 {time.time()-t:.1f}s：{type(e).__name__}: {str(e)[:300]}")

def head(df, n=3):
    return f"行数 {len(df)}；列 {list(df.columns)}\n{df.head(n).to_string()}\n...\n{df.tail(2).to_string()}"

probe("申万成分 index_component_sw 801160", lambda: head(ak.index_component_sw(symbol="801160")))
probe("同花顺年报 stock_financial_abstract_ths 600900 按年度", lambda: head(ak.stock_financial_abstract_ths(symbol="600900", indicator="按年度")))
probe("同花顺单季 stock_financial_abstract_ths 600900 按单季度", lambda: head(ak.stock_financial_abstract_ths(symbol="600900", indicator="按单季度")))
probe("新浪分红 stock_history_dividend_detail 600900", lambda: head(ak.stock_history_dividend_detail(symbol="600900", indicator="分红")))

def get(u, ref):
    r = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Referer": ref})
    return urllib.request.urlopen(r, timeout=20).read().decode("gbk", "ignore")

def tq():
    t = get("https://qt.gtimg.cn/q=sh600900,sz000333,sh601899", "https://gu.qq.com")
    out = []
    for line in t.strip().split(";"):
        if "~" not in line: continue
        f = line.split("~")
        out.append(f"{f[1]} {f[2]} 价{f[3]} 成交额(万){f[37]} 总市值(亿){f[45]} 流通市值(亿){f[44]} 字段数{len(f)}")
    return "\n".join(out)
probe("腾讯实时行情 qt.gtimg.cn（价格、总市值）", tq)

def tk():
    t = get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600900,day,,,320,", "https://gu.qq.com")
    d = json.loads(t)["data"]["sh600900"]
    rows = d.get("day") or d.get("qfqday")
    return f"行数 {len(rows)}；每行字段数 {len(rows[-1])}；末行 {rows[-1]}；首行 {rows[0]}"
probe("腾讯日K（看有没有成交额字段）", tk)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")
