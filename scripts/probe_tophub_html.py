#!/usr/bin/env python3
"""临时：保存 tophub 财经分类页和一个节点页的原始网页，用来写解析。"""
import os
import urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
os.makedirs("logs/tophub样本", exist_ok=True)
for name, url in [("分类页", "https://tophub.today/c/finance"), ("节点页", "https://tophub.today/n/0MdKam4ow1")]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://tophub.today/"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    open(f"logs/tophub样本/{name}.html", "w", encoding="utf-8").write(html)
    print(name, len(html))
