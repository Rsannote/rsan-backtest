#!/usr/bin/env python3
"""每日社区讨论摘录：热门帖子的正文 + 回复。

在 GitHub 服务器上随 community-sentiment.yml 每天收盘后运行，也可以手动：
    python3 scripts/fetch_discussion.py [--posts 20]

来源：
- 上证指数股吧：评论数最多的前 N 帖，正文 + 最新 30 条回复（含点赞数）
- 集思录：tophub 集思录热门榜前 10 帖，正文 + 全部回复（含赞同数）
（雪球帖子页对 GitHub 服务器拒绝访问，不在这里。）

产出 data/社区情绪/<日期>/：
- 讨论摘录.md       给人和 Claude 直接读：每帖标题、阅读/评论、正文节选、高赞回复
- 讨论原始.jsonl    一行一帖，完整正文和回复，方便以后做词频、多空统计
每个帖子单独容错；每次请求间隔 1.5 秒，一天约 50 次请求。
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
out_dir = os.path.join("data", "社区情绪", now.strftime("%Y-%m-%d"))
os.makedirs(out_dir, exist_ok=True)


def get(url, referer=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer or url})
    with urllib.request.urlopen(req, timeout=25) as r:
        body = r.read().decode("utf-8", "replace")
    time.sleep(1.5)
    return body


def clean(s):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def js_obj(page, var):
    i = page.find(f"var {var}=")
    if i < 0:
        i = page.find(f"var {var} =")
    if i < 0:
        return None
    return json.JSONDecoder().raw_decode(page[page.find("{", i):])[0]


# ---------- 股吧 ----------

def guba_posts(n):
    lst = get("https://guba.eastmoney.com/list,zssh000001,99.html", "https://guba.eastmoney.com/")
    arts = (js_obj(lst, "article_list") or {}).get("re") or []
    arts = [a for a in arts if not a.get("post_top_status")]  # 去掉置顶
    arts.sort(key=lambda a: a.get("post_comment_count") or 0, reverse=True)
    posts = []
    for a in arts[:n]:
        pid, bar = a["post_id"], a.get("stockbar_code") or "zssh000001"
        url = f"https://guba.eastmoney.com/news,{bar},{pid}.html"
        p = {"来源": "股吧", "吧": a.get("stockbar_name"), "标题": clean(a.get("post_title")),
             "作者": a.get("user_nickname"), "发帖时间": a.get("post_publish_time"),
             "阅读": a.get("post_click_count"), "评论": a.get("post_comment_count"),
             "多空标记": a.get("bullish_bearish"), "链接": url, "正文": "", "回复": [], "错误": ""}
        try:
            art = js_obj(get(url, "https://guba.eastmoney.com/"), "post_article") or {}
            p["正文"] = clean(art.get("post_content") or art.get("post_abstract") or "")
        except Exception as e:  # noqa: BLE001
            p["错误"] += f"正文：{type(e).__name__} "
        try:
            d = json.loads(get("https://gbapi.eastmoney.com/reply/api/Reply/ArticleNewReplyList"
                               f"?postid={pid}&sort=1&sorttype=1&p=1&ps=30&plat=Web"
                               "&version=300&product=Guba&deviceid=0", url))
            for r in d.get("re") or []:
                p["回复"].append({"内容": clean(r.get("reply_text")),
                                "时间": r.get("reply_time") or r.get("reply_publish_time"),
                                "点赞": r.get("reply_like_count") or 0,
                                "楼中楼": len(r.get("child_replys") or [])})
        except Exception as e:  # noqa: BLE001
            p["错误"] += f"回复：{type(e).__name__}"
        posts.append(p)
    return posts


# ---------- 集思录 ----------

def jisilu_posts(n):
    files = sorted(glob.glob("data/社区情绪/*/tophub财经热榜.csv"))
    if not files:
        return []
    import csv
    with open(files[-1], encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["来源"] == "集思录"][:n]
    posts = []
    for r in rows:
        p = {"来源": "集思录", "标题": r["标题"], "链接": r["链接"], "正文": "", "回复": [], "错误": ""}
        try:
            h = get(r["链接"], "https://www.jisilu.cn/")
            m = re.search(r'class="aw-question-detail-txt markitup-box"\s*>(.*?)'
                          r'(?:<div class="aw-comment-upload-img-list|</div>)', h, re.S)
            p["正文"] = clean(m.group(1)) if m else ""
            for it in re.split(r'<div class="aw-item"[^>]*id="answer_list_\d+">', h)[1:]:
                v = re.search(r"aw-vote-count[^>]*>(-?\d+)<", it)
                b = re.search(r'class="markitup-box"\s*>(.*?)</div>', it, re.S)
                u = re.search(r'class="aw-user-name"[^>]*>(.*?)</a>', it, re.S)
                if b:
                    p["回复"].append({"内容": clean(b.group(1)), "赞同": int(v.group(1)) if v else 0,
                                    "作者": clean(u.group(1)) if u else ""})
        except Exception as e:  # noqa: BLE001
            p["错误"] = f"{type(e).__name__} {str(e)[:100]}"
        posts.append(p)
    return posts


# ---------- 输出 ----------

def cut(s, n):
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def to_md(posts):
    lines = [f"# 社区讨论摘录 {now:%Y-%m-%d}", "",
             f"抓取时间 {now:%H:%M}（北京时间）。股吧按评论数取前列，集思录取 tophub 热门榜。"
             "回复按点赞/赞同排序，各取前 10 条。完整内容见 讨论原始.jsonl。", ""]
    for src in ["股吧", "集思录"]:
        ps = [p for p in posts if p["来源"] == src]
        if not ps:
            continue
        lines += [f"## {src}（{len(ps)} 帖）", ""]
        for i, p in enumerate(ps, 1):
            meta = []
            if p.get("阅读") is not None:
                meta.append(f"阅读 {p['阅读']}")
            if p.get("评论") is not None:
                meta.append(f"评论 {p['评论']}")
            meta.append(f"抓到回复 {len(p['回复'])} 条")
            lines.append(f"### {i}. {p['标题']}")
            lines.append("　".join(meta) + (f"　（{p['错误'].strip()}）" if p["错误"] else ""))
            if p["正文"] and p["正文"] != p["标题"]:
                lines.append(f"> {cut(p['正文'], 400)}")
            key = "点赞" if src == "股吧" else "赞同"
            for r in sorted(p["回复"], key=lambda x: x.get(key) or 0, reverse=True)[:10]:
                if r["内容"]:
                    lines.append(f"- （{key}{r.get(key) or 0}）{cut(r['内容'], 120)}")
            lines.append("")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", type=int, default=20)
    args = ap.parse_args()
    posts, errs = [], []
    for name, fn, n in [("股吧", guba_posts, args.posts), ("集思录", jisilu_posts, 10)]:
        try:
            posts += fn(n)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}：{type(e).__name__} {str(e)[:150]}")
    with open(os.path.join(out_dir, "讨论原始.jsonl"), "w", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "讨论摘录.md"), "w", encoding="utf-8") as f:
        f.write(to_md(posts))
    with open(os.path.join(out_dir, "运行记录.txt"), "a", encoding="utf-8") as f:
        cnt = {s: sum(1 for p in posts if p["来源"] == s) for s in ["股吧", "集思录"]}
        rep = sum(len(p["回复"]) for p in posts)
        f.write(f"讨论摘录：股吧 {cnt['股吧']} 帖、集思录 {cnt['集思录']} 帖，回复共 {rep} 条\n")
        if errs:
            f.write("讨论摘录失败：\n" + "\n".join(errs) + "\n")
    print(len(posts), "帖", errs)


if __name__ == "__main__":
    main()
