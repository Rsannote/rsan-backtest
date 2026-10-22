#!/usr/bin/env python3
"""保存两个 tophub 榜单页的原始 HTML，用来确认页面结构（一次性调试）。"""
import os, time, urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
os.makedirs("logs/tophub_node_raw", exist_ok=True)
for nid in ["4MdA0mbvxD", "5PdMXp0vmg", "L4MdA5ldxD", "yjvQkmjebg"]:
    req = urllib.request.Request(f"https://tophub.today/n/{nid}", headers={"User-Agent": UA, "Referer": "https://tophub.today/"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type")
    open(f"logs/tophub_node_raw/{nid}.html", "wb").write(raw)
    print(nid, len(raw), ctype)
    time.sleep(2)
