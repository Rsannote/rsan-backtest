#!/usr/bin/env python3
"""每日社区情绪数据：热度榜 + 社区讨论标题。

在 GitHub 服务器上定时运行（.github/workflows/community-sentiment.yml），也可以手动：
    python3 scripts/fetch_sentiment.py

产出 data/社区情绪/<日期>/：
- 东财人气榜.csv       前100名（akshare）
- 东财飙升榜.csv       前50名
- 同花顺热股榜.csv     前100名，含热度值、涨跌幅、标签
- 同花顺热门概念.csv   前20个概念
- 股吧上证吧帖子.csv   上证指数吧最新帖子标题、阅读、评论
- tophub财经热榜.csv   tophub 财经分类页：股吧热榜、雪球今日话题、集思录、格隆汇、华尔街见闻等
- 微博热搜.csv         实时热搜（看大众情绪和财经话题有没有出圈）
- 运行记录.txt         每个源成功几行、失败原因
每个源单独容错，失败会隔 10–20 秒重试，最多 3 次，一个失败不影响其他。每天只请求一次，频率很低。

已验证不可用（2026-10-22 探测，见 logs/情绪源探测.txt）：雪球直连接口、财联社电报、淘股吧、百度股市通。
雪球内容改从 tophub 的“雪球今日话题”拿。
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.request

import pandas as pd

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
today = now.strftime("%Y-%m-%d")
out_dir = os.path.join("data", "社区情绪", today)
os.makedirs(out_dir, exist_ok=True)


def get(url, referer=None, data=None, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


# ---------- 各数据源：返回 DataFrame ----------

def em_hot():
    import akshare as ak
    return ak.stock_hot_rank_em()


def em_up():
    body = json.dumps({"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
                       "marketType": "", "pageNo": 1, "pageSize": 50}).encode()
    txt = get("https://emappdata.eastmoney.com/stockrank/getAllHisRcList", data=body,
              referer="https://guba.eastmoney.com/rank/",
              headers={"Content-Type": "application/json"})
    return pd.DataFrame(json.loads(txt).get("data") or [])


def ths_stock():
    d = json.loads(get("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
                       "?stock_type=a&type=hour&list_type=normal",
                       referer="https://eq.10jqka.com.cn/"))
    return pd.json_normalize(d["data"]["stock_list"])


def ths_concept():
    d = json.loads(get("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/plate"
                       "?type=concept", referer="https://eq.10jqka.com.cn/"))
    return pd.json_normalize(d["data"]["plate_list"])


def guba_sh():
    txt = get("https://guba.eastmoney.com/list,zssh000001,99.html",
              referer="https://guba.eastmoney.com/")
    rows = []
    for m in re.finditer(r'\{[^{}]*?"post_title":"[^{}]*?\}', txt):
        try:
            o = json.loads(m.group(0))
        except ValueError:
            continue
        rows.append({"标题": clean(o.get("post_title")),
                     "阅读": o.get("post_click_count"),
                     "评论": o.get("post_comment_count"),
                     "发帖时间": o.get("post_publish_time"),
                     "最后回复": o.get("post_last_time"),
                     "帖子id": o.get("post_id")})
    if not rows:  # 结构变了时退回只取标题
        rows = [{"标题": clean(t)} for t in re.findall(r'"post_title":"(.*?)"', txt)]
    return pd.DataFrame(rows)


def tophub_finance():
    txt = get("https://tophub.today/c/finance", referer="https://tophub.today/")
    rows = []
    for card in re.split(r'<div class="cc-cd" id="node-\d+">', txt)[1:]:
        src = re.search(r'class="cc-cd-lb">(.*?)</div>', card, re.S)
        lst = re.search(r'cc-cd-sb-st">(.*?)</span>', card, re.S)
        items = re.findall(r'<a href="([^"]+)"[^>]*>\s*<div class="cc-cd-cb-ll">\s*'
                           r'<span class="s[^"]*">(.*?)</span>\s*<span class="t">(.*?)</span>\s*'
                           r'<span class="e">(.*?)</span>', card, re.S)
        for url, rank, title, extra in items:
            rows.append({"来源": clean(src.group(1)) if src else "",
                         "榜单": clean(lst.group(1)) if lst else "",
                         "排名": clean(rank), "标题": clean(title),
                         "热度": clean(extra), "链接": url})
    return pd.DataFrame(rows)


def weibo():
    d = json.loads(get("https://weibo.com/ajax/side/hotSearch", referer="https://weibo.com/"))
    rows = [{"排名": i + 1, "话题": x.get("word"), "热度": x.get("num"),
             "标签": x.get("label_name")} for i, x in enumerate(d["data"]["realtime"])]
    return pd.DataFrame(rows)


sources = [
    ("东财人气榜", em_hot), ("东财飙升榜", em_up),
    ("同花顺热股榜", ths_stock), ("同花顺热门概念", ths_concept),
    ("股吧上证吧帖子", guba_sh), ("tophub财经热榜", tophub_finance),
    ("微博热搜", weibo),
]

ok, failed = [], []
def with_retry(fn, tries=3):
    for i in range(tries):
        try:
            df = fn()
            if df is None or df.empty:
                raise ValueError("返回空数据")
            return df
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(10 * (i + 1))


for name, fn in sources:
    try:
        df = with_retry(fn)
        df.to_csv(os.path.join(out_dir, f"{name}.csv"), index=False, encoding="utf-8-sig")
        ok.append(f"{name}（{len(df)}行）")
    except Exception as e:  # noqa: BLE001
        failed.append(f"{name}：{type(e).__name__} {str(e)[:150]}")
    time.sleep(1.5)

with open(os.path.join(out_dir, "运行记录.txt"), "w", encoding="utf-8") as f:
    f.write(f"抓取时间：{now:%Y-%m-%d %H:%M}（北京时间）\n成功：{'、'.join(ok) or '无'}\n")
    f.write("失败：\n" + ("\n".join(failed) if failed else "无") + "\n")

print("成功：", ok)
print("失败：", failed)
sys.exit(0 if ok else 1)
