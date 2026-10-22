#!/usr/bin/env python3
"""拉取今日热榜（tophub.today）全部分类、全部分页的榜单目录。

只读、低频（每页间隔 2 秒）。产出：
- data/信息源/tophub目录.csv   一行一个榜单：分类、网站、榜单名、节点ID、链接、首条标题
- logs/tophub目录.txt          每个分类的页数和榜单数
用法：python3 scripts/tophub_catalog.py [--max-pages 60]
"""
import argparse
import csv
import html
import os
import re
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CATS = ["news", "tech", "ent", "community", "shopping", "finance", "developer", "brief", "ai",
        "epaper", "design", "university", "organization", "blog", "wxmp", "widget"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://tophub.today/"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        print("HTTP", e.code, url)
    time.sleep(2)
    return body


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse(page):
    """每个榜单卡片是 <div class="cc-cd" id="node-xxx"> ... </div>，里面有网站名、榜单名、条目。"""
    out = []
    for block in re.split(r'<div class="cc-cd" id="node-\d+">', page)[1:]:
        link = re.search(r'href="(?:https://tophub\.today)?(/n/[A-Za-z0-9]+)"', block)
        site = re.search(r'class="cc-cd-lb">(.*?)</div>', block, re.S)
        sub = re.search(r'cc-cd-sb-st">(.*?)</span>', block, re.S)
        first = re.search(r'<span class="t">(.*?)</span>', block, re.S)
        if not link:
            continue
        out.append({
            "网站": clean(site.group(1)) if site else "",
            "榜单名": clean(sub.group(1)) if sub else "",
            "节点ID": link.group(1).split("/")[-1],
            "链接": "https://tophub.today" + link.group(1),
            "首条标题": clean(first.group(1))[:60] if first else "",
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=60)
    args = ap.parse_args()
    os.makedirs("data/信息源", exist_ok=True)
    rows, log, seen = [], [], set()
    for cat in CATS:
        n_cat, pages = 0, 0
        for p in range(1, args.max_pages + 1):
            body = get(f"https://tophub.today/c/{cat}?p={p}")
            items = parse(body)
            new = [r for r in items if (cat, r["节点ID"]) not in seen]
            if not new:
                break
            pages = p
            for r in new:
                seen.add((cat, r["节点ID"]))
                rows.append({"分类": cat, **r})
            n_cat += len(new)
        log.append(f"{cat}\t{pages}页\t{n_cat}个榜单")
        print(log[-1], flush=True)
    with open("data/信息源/tophub目录.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["分类", "网站", "榜单名", "节点ID", "链接", "首条标题"])
        w.writeheader()
        w.writerows(rows)
    os.makedirs("logs", exist_ok=True)
    with open("logs/tophub目录.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(log) + f"\n合计 {len(rows)} 条（同一榜单可出现在多个分类）\n")


if __name__ == "__main__":
    main()
