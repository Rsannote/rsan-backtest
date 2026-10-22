#!/usr/bin/env python3
"""临时探测：各社区帖子正文和评论能否从 GitHub 服务器读到。
取当天 tophub 财经热榜里每个来源的前 2 条链接 + 上证股吧前 2 帖，读正文，结果写 logs/正文探测.txt。
"""
import glob
import html
import re
import time
import urllib.request

import pandas as pd

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def get(url, referer=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer or url})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def text_of(h):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    for pat in [r'(?is)<div[^>]+class="[^"]*(article-body|articleBody|xeditor|newstext|'
                r'article_content|txtinfos|aqq-content|article-content|post_body|stockcodec|'
                r'detail|content)[^"]*"[^>]*>(.*?)</div>\s*</div>']:
        m = re.search(pat, h)
        if m and len(re.sub("<[^>]+>", "", m.group(2))) > 80:
            h = m.group(2)
            break
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"\s+", " ", t).strip()


files = sorted(glob.glob("data/社区情绪/*/tophub财经热榜.csv"))
t = pd.read_csv(files[-1])
targets = []
for src in ["股吧", "雪球", "集思录", "格隆汇", "华尔街见闻", "21财经"]:
    for _, r in t[t["来源"] == src].head(2).iterrows():
        targets.append((src, r["标题"], r["链接"]))

# 上证吧帖子：从列表页取两个帖子链接
try:
    lst = get("https://guba.eastmoney.com/list,zssh000001,99.html", "https://guba.eastmoney.com/")
    ids = re.findall(r'"post_id":(\d+)', lst)[:2]
    for i in ids:
        targets.append(("上证吧", "", f"https://guba.eastmoney.com/news,zssh000001,{i}.html"))
    open("logs/股吧列表页样本.txt", "w", encoding="utf-8").write(lst[:20000])
except Exception as e:  # noqa: BLE001
    targets.append(("上证吧", f"列表失败 {e}", ""))

out = []
for src, title, url in targets:
    if not url:
        out.append(f"[失败] {src}：{title}")
        continue
    try:
        h = get(url)
        body = text_of(h)
        ok = "成功" if len(body) > 200 else "疑似空壳"
        out.append(f"[{ok}] {src}｜{str(title)[:25]}｜{len(h)}字节网页，正文约{len(body)}字\n"
                   f"    开头：{body[:220]}")
    except Exception as e:  # noqa: BLE001
        out.append(f"[失败] {src}｜{str(title)[:25]}｜{type(e).__name__} {str(e)[:120]}")
    time.sleep(2)

open("logs/正文探测.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("\n".join(out))
