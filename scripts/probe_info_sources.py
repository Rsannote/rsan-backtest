#!/usr/bin/env python3
"""探测信息源监控（system/信息源白名单.json）里 Reddit、知乎能否从 GitHub 服务器访问。
只读、低频，每个地址请求一次，间隔 2 秒。结果写到 logs/信息源探测.txt。
"""
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BOT_UA = "linux:rsan-info-monitor:v0.1 (personal research)"


def probe(name, url, ua=UA, referer=None):
    h = {"User-Agent": ua, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # 网络错误
        status, body = "错误", repr(e)
    time.sleep(2)
    # 粗判内容是否有效
    hint = ""
    if isinstance(status, int) and status == 200:
        if "<entry>" in body or "<item>" in body:
            hint = f"RSS条目 {body.count('<entry>') + body.count('<item>')}"
        elif body.lstrip().startswith("{"):
            try:
                d = json.loads(body)
                if isinstance(d, dict):
                    data = d.get("data")
                    hint = f"JSON data条数 {len(data)}" if isinstance(data, (list, dict)) else "JSON " + ",".join(list(d)[:5])
            except Exception:
                hint = "JSON解析失败"
        else:
            hint = "HTML " + ("含登录/验证" if any(k in body for k in ("signin", "登录", "captcha", "验证")) else "")
    lines.append(f"{name}\t{status}\t{len(body)}字节\t{hint}\t{url}")
    return status, body


lines = []
now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

# Reddit：RSS、JSON、old 域名，浏览器 UA 和规范 bot UA 各一次
for sub in ["LocalLLaMA", "wallstreetbets"]:
    probe(f"reddit rss {sub}", f"https://www.reddit.com/r/{sub}/top/.rss?t=week")
    probe(f"reddit rss {sub} botUA", f"https://www.reddit.com/r/{sub}/top/.rss?t=week", ua=BOT_UA)
    probe(f"reddit json {sub} botUA", f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=10", ua=BOT_UA)
    probe(f"old.reddit rss {sub}", f"https://old.reddit.com/r/{sub}/top/.rss?t=week", ua=BOT_UA)

# 知乎：话题搜索、话题精华接口、话题页、热榜接口
q = urllib.parse.quote("股票")
probe("知乎 话题搜索", f"https://www.zhihu.com/api/v4/search_v3?t=topic&q={q}&offset=0&limit=5",
      referer="https://www.zhihu.com/")
probe("知乎 话题页 股票", "https://www.zhihu.com/topic/19554196/hot", referer="https://www.zhihu.com/")
probe("知乎 话题精华接口", "https://www.zhihu.com/api/v4/topics/19554196/feeds/essence?limit=10",
      referer="https://www.zhihu.com/")
probe("知乎 热榜接口", "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=10",
      referer="https://www.zhihu.com/")
probe("tophub 知乎热榜", "https://tophub.today/n/mproPpoq6O", referer="https://tophub.today/")

# 公共 RSSHub 实例（备用路子）
probe("rsshub reddit", "https://rsshub.app/reddit/subreddit/LocalLLaMA")
probe("rsshub 知乎热榜", "https://rsshub.app/zhihu/hot")

with open("logs/信息源探测.txt", "w", encoding="utf-8") as f:
    f.write(f"# 信息源探测 {now:%Y-%m-%d %H:%M}（北京时间，GitHub 服务器）\n")
    f.write("名称\t状态\t大小\t内容\t地址\n")
    f.write("\n".join(lines) + "\n")
print("\n".join(lines))
