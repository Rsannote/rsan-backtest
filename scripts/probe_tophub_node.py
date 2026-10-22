#!/usr/bin/env python3
"""保存几个解析失败的 tophub 榜单页原始 HTML，用来修解析（一次性调试）。"""
import os, time, urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
os.makedirs("logs/tophub_node_raw", exist_ok=True)
for nid in ["X12owXzvNV", "2me3N3xdwj", "mproPpoq6O", "12owJyweNV", "ENeYylkeY4", "KGoRlY5dl6"]:
    req = urllib.request.Request(f"https://tophub.today/n/{nid}", headers={"User-Agent": UA, "Referer": "https://tophub.today/"})
    with urllib.request.urlopen(req, timeout=25) as r:
        open(f"logs/tophub_node_raw/{nid}.html", "wb").write(r.read())
    time.sleep(2)
