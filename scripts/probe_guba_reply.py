#!/usr/bin/env python3
"""临时：探测股吧回复接口。结果写 logs/股吧回复探测.txt。"""
import json, re, urllib.parse, urllib.request
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
def req(u, data=None, ref="https://guba.eastmoney.com/"):
    h = {"User-Agent": UA, "Referer": ref, "Origin": "https://guba.eastmoney.com"}
    if data is not None:
        data = urllib.parse.urlencode(data).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
    return urllib.request.urlopen(urllib.request.Request(u, data=data, headers=h), timeout=20).read().decode("utf-8", "replace")
lst = req("https://guba.eastmoney.com/list,zssh000001,99.html")
arts = json.JSONDecoder().raw_decode(lst[lst.find("var article_list=") + 17:])[0]["re"]
a = max(arts, key=lambda x: x.get("post_comment_count") or 0)
pid, bar = a["post_id"], a.get("stockbar_code")
out = [f"帖子 {pid} 吧 {bar} 评论数 {a.get('post_comment_count')} 字段：{sorted(a.keys())[:60]}"]
ref = f"https://guba.eastmoney.com/news,{bar},{pid}.html"
tries = []
for path in ["reply/api/Reply/ArticleNewReplyList", "reply/api/Reply/ArticleHotReply"]:
    p = f"postid={pid}&sort=1&sorttype=1&p=1&ps=30"
    tries.append((f"getData {path}", "https://guba.eastmoney.com/api/getData?code=&path=" + path,
                  {"param": p, "path": path, "env": "2", "origin": "", "version": "2022", "product": "Guba", "plat": "Web"}))
tries.append(("gbapi GET", f"https://gbapi.eastmoney.com/reply/api/Reply/ArticleNewReplyList?postid={pid}&sort=1&sorttype=1&p=1&ps=30&plat=Web&version=300&product=Guba&deviceid=0", None))
for name, u, d in tries:
    try:
        t = req(u, d, ref)
        out.append(f"[返回] {name}：{len(t)}字节：{t[:600]}")
    except Exception as e:
        out.append(f"[失败] {name}：{type(e).__name__} {str(e)[:150]}")
open("logs/股吧回复探测.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("\n".join(out))
