#!/usr/bin/env python3
"""探测哪些热榜/讨论类数据源能从 GitHub 服务器访问。
只读、低频，每个源请求一两次。结果写到 logs/情绪源探测.txt。
"""
import http.cookiejar
import json
import re
import ssl
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
ctx = ssl.create_default_context()


def get(url, referer=None, data=None, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with opener.open(req, timeout=20) as r:
        return r.status, r.read().decode("utf-8", "replace")


lines = []


def log(s):
    print(s)
    lines.append(s)


def probe(name, fn):
    try:
        res = fn()
        log(f"[成功] {name}：{res}")
    except Exception as e:  # noqa: BLE001
        log(f"[失败] {name}：{type(e).__name__} {str(e)[:160]}")
    time.sleep(1.5)


def j(url, **kw):
    st, body = get(url, **kw)
    return json.loads(body)


def take(items, key_fn, n=5):
    return f"{len(items)}条，例：" + " | ".join(str(key_fn(x))[:30] for x in items[:n])


# 同花顺热榜
def ths_hot():
    d = j("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
          "?stock_type=a&type=hour&list_type=normal",
          referer="https://eq.10jqka.com.cn/")
    items = d["data"]["stock_list"]
    return take(items, lambda x: f"{x.get('order')}.{x.get('name')}")


def ths_concept():
    d = j("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/plate"
          "?type=concept", referer="https://eq.10jqka.com.cn/")
    items = d["data"]["plate_list"]
    return take(items, lambda x: f"{x.get('order')}.{x.get('name')}")


# 东财飙升榜（换浏览器头重试）
def em_up():
    body = json.dumps({"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
                       "marketType": "", "pageNo": 1, "pageSize": 50}).encode()
    st, txt = get("https://emappdata.eastmoney.com/stockrank/getAllHisRcList",
                  data=body, referer="https://guba.eastmoney.com/rank/",
                  headers={"Content-Type": "application/json"})
    d = json.loads(txt)
    return take(d.get("data") or [], lambda x: x.get("sc"))


# 雪球：先访问首页拿游客 cookie，再取热股
def xq_hot():
    get("https://xueqiu.com/")
    d = j("https://stock.xueqiu.com/v5/stock/hot_stock/list.json?size=30&_type=10&type=10",
          referer="https://xueqiu.com/")
    items = d["data"]["items"]
    return take(items, lambda x: x.get("name"))


def xq_posts():
    d = j("https://xueqiu.com/statuses/hot/listV2.json?since_id=-1&max_id=-1&size=15",
          referer="https://xueqiu.com/")
    items = d.get("items") or d.get("list") or []
    return take(items, lambda x: (x.get("original_status") or x).get("title")
                or re.sub("<[^>]+>", "", (x.get("original_status") or x).get("text", "")))


# 财联社
def cls_tele():
    d = j("https://www.cls.cn/nodeapi/telegraphList?rn=20", referer="https://www.cls.cn/telegraph")
    items = d["data"]["roll_data"]
    return take(items, lambda x: x.get("title") or x.get("content"))


def cls_hot():
    d = j("https://www.cls.cn/v2/article/hot/list?app=CailianpressWeb&os=web&sv=8.4.6",
          referer="https://www.cls.cn/")
    items = d.get("data") or []
    return take(items, lambda x: x.get("title"))


# 东财股吧热帖
def guba_hot():
    st, txt = get("https://guba.eastmoney.com/list,zssh000001,99.html",
                  referer="https://guba.eastmoney.com/")
    titles = re.findall(r'"post_title":"(.*?)"', txt)
    if not titles:
        raise ValueError(f"页面{len(txt)}字节，未解析到帖子")
    return take(titles, lambda x: x)


# 淘股吧
def tgb():
    st, txt = get("https://www.taoguba.com.cn/", referer="https://www.taoguba.com.cn/")
    titles = re.findall(r'<a[^>]+href="[^"]*Article/\d+[^"]*"[^>]*>([^<]{6,60})</a>', txt)
    if not titles:
        raise ValueError(f"页面{len(txt)}字节，未解析到帖子")
    return take(titles, lambda x: x.strip())


# 微博热搜
def weibo():
    d = j("https://weibo.com/ajax/side/hotSearch", referer="https://weibo.com/")
    items = d["data"]["realtime"]
    return take(items, lambda x: x.get("word"))


# 百度股市通热搜
def baidu_stock():
    d = j("https://finance.pae.baidu.com/vapi/v1/hotrank?tn=wisexmlnew&dsp=iphone"
          "&product=stock&style=tablelist&market=ab&type=hour&day=&hour=&pn=0&rn=20",
          referer="https://gushitong.baidu.com/")
    rows = d["Result"]["body"]
    return take(rows, lambda x: x)


# 知乎热榜
def zhihu():
    d = j("https://api.zhihu.com/topstory/hot-lists/total?limit=20",
          referer="https://www.zhihu.com/hot")
    items = d["data"]
    return take(items, lambda x: x["target"].get("title"))


# tophub 聚合热榜：财经分类页 + 抽几个财经节点
tophub_nodes = []


def tophub_cat():
    st, txt = get("https://tophub.today/c/finance", referer="https://tophub.today/")
    nodes = re.findall(r'href="(/n/[A-Za-z0-9]+)"[^>]*>.*?<div class="cc-cd-lb">.*?'
                       r'<span>(.*?)</span>', txt, re.S)
    if not nodes:
        nodes = [(m, m) for m in sorted(set(re.findall(r'href="(/n/[A-Za-z0-9]+)"', txt)))]
    if not nodes:
        raise ValueError(f"页面{len(txt)}字节，未解析到榜单")
    tophub_nodes.extend(nodes)
    return f"{len(nodes)}个榜单：" + "、".join(re.sub("<[^>]+>|\\s+", "", n[1]) for n in nodes[:40])


def tophub_node(path):
    def f():
        st, txt = get("https://tophub.today" + path, referer="https://tophub.today/c/finance")
        title = re.search(r"<title>(.*?)</title>", txt, re.S)
        items = re.findall(r'<td class="al"><a[^>]*>(.*?)</a>', txt, re.S)
        if not items:
            raise ValueError(f"页面{len(txt)}字节，未解析到条目")
        return (title.group(1).strip()[:30] if title else path) + "，" + take(
            items, lambda x: re.sub("<[^>]+>", "", x).strip())
    return f


for name, fn in [
    ("同花顺热股榜", ths_hot), ("同花顺热门概念", ths_concept),
    ("东财飙升榜（重试）", em_up),
    ("雪球热股", xq_hot), ("雪球热帖", xq_posts),
    ("财联社电报", cls_tele), ("财联社热门文章", cls_hot),
    ("东财股吧上证吧帖子", guba_hot), ("淘股吧首页", tgb),
    ("微博热搜", weibo), ("百度股市通热搜", baidu_stock), ("知乎热榜", zhihu),
    ("tophub财经分类", tophub_cat),
]:
    probe(name, fn)

for path, nm in tophub_nodes[:8]:
    probe("tophub节点 " + re.sub("<[^>]+>|\\s+", "", nm)[:20], tophub_node(path))

import os  # noqa: E402
os.makedirs("logs", exist_ok=True)
with open("logs/情绪源探测.txt", "w", encoding="utf-8") as fh:
    fh.write(time.strftime("%Y-%m-%d %H:%M UTC\n", time.gmtime()) + "\n".join(lines) + "\n")
