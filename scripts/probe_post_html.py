#!/usr/bin/env python3
"""临时：保存集思录、股吧帖子页原始网页，用来写正文和回复的提取。"""
import glob, os, re, urllib.request
import pandas as pd
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
def get(u, ref=None):
    r = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": ref or u})
    return urllib.request.urlopen(r, timeout=25).read().decode("utf-8", "replace")
os.makedirs("logs/正文样本", exist_ok=True)
t = pd.read_csv(sorted(glob.glob("data/社区情绪/*/tophub财经热榜.csv"))[-1])
urls = {"集思录": t[t["来源"] == "集思录"]["链接"].iloc[0],
        "财富号": t[t["来源"] == "股吧"]["链接"].iloc[1]}
lst = get("https://guba.eastmoney.com/list,zssh000001,99.html", "https://guba.eastmoney.com/")
open("logs/正文样本/股吧列表页.html", "w", encoding="utf-8").write(lst)
m = re.search(r'"post_id":(\d+)', lst)
urls["股吧帖子"] = f"https://guba.eastmoney.com/news,zssh000001,{m.group(1)}.html"
for k, u in urls.items():
    try:
        h = get(u)
        open(f"logs/正文样本/{k}.html", "w", encoding="utf-8").write(h)
        print(k, u, len(h))
    except Exception as e:
        print(k, u, "失败", e)
