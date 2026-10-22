#!/usr/bin/env python3
"""新闻库：抓财经快讯、官方政策发布和股票池公司公告，按 system/新闻归类.json 初分后入库。

在 GitHub 服务器上定时运行（.github/workflows/fetch-news.yml），也可以手动：
    python3 scripts/fetch_news.py [--hours 36]

产出：
- data/新闻/<年-月>/<日期>.jsonl   只追加，一行一条；按北京时间的发布日期分文件。
- data/新闻/最新.md                最近 24 小时重要度≥2 的条目和各标签计数，给人和 Claude 快速看。
- data/新闻/状态.json              各来源最近一次抓取条数、成败。
- data/新闻/巨潮机构代码.json       巨潮公告查询要用的 orgId 缓存。

一条记录：id, time（北京时间）, date, source, level（一级/二级）, kind（快讯/政策/公告）,
title, text（最多 600 字）, url, tags{宏观,驱动,行业,个股}, score（1–3）, flag（来源自带的重要标记）。

去重：同一来源同一 id 不重复入库；不同来源内容几乎相同的快讯，只保留先入库的那条，后来的记到 dup_of。
"""
import argparse
import csv
import datetime as dt
import glob
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS = os.path.join(ROOT, "data", "新闻")
RULES = os.path.join(ROOT, "system", "新闻归类.json")
POOL = os.path.join(ROOT, "data", "股票池", "股票.csv")
BJ = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def now_bj():
    return dt.datetime.now(BJ)


def http(url, headers=None, data=None, timeout=25, tries=2):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=timeout) as r:
                raw = r.read()
                cs = r.headers.get_content_charset() or "utf-8"
                return raw.decode(cs, errors="replace")
        except Exception as e:
            err = e
            time.sleep(2 + 3 * i)
    raise err


def strip_html(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def mk(source, sid, t, title, text, url, level="二级", kind="快讯", flag=False):
    title = strip_html(title)
    text = strip_html(text)
    if not title:
        m = re.match(r"^【([^】]{4,80})】", text)
        title = m.group(1) if m else text[:60]
    return {"id": f"{source}:{sid}", "time": t.strftime("%Y-%m-%d %H:%M"), "date": t.strftime("%Y-%m-%d"),
            "source": source, "level": level, "kind": kind, "title": title, "text": text[:600],
            "url": url or "", "flag": bool(flag)}


# ---------------- 快讯 ----------------

def src_sina(cutoff):
    out = []
    for page in range(1, 16):
        js = json.loads(http(f"https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size=100&zhibo_id=152&tag_id=0&dire=f&dpc=1&type=0"))
        lst = js["result"]["data"]["feed"]["list"]
        if not lst:
            break
        for it in lst:
            t = dt.datetime.strptime(it["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ)
            if t < cutoff:
                continue
            tags = [x.get("name", "") for x in (it.get("tag") or [])]
            out.append(mk("新浪7x24", it["id"], t, "", it.get("rich_text", ""), it.get("docurl", ""),
                          flag=bool(it.get("is_focus")) or "焦点" in tags))
        oldest = dt.datetime.strptime(lst[-1]["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ)
        if oldest < cutoff:
            break
        time.sleep(0.5)
    return out


def src_wscn(cutoff, channel, name):
    out, cursor = [], ""
    for _ in range(15):
        url = f"https://api-one-wscn.awtmt.com/apiv1/content/lives?channel={channel}&client=pc&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        d = json.loads(http(url))["data"]
        items = d.get("items") or []
        if not items:
            break
        for it in items:
            t = dt.datetime.fromtimestamp(it["display_time"], BJ)
            if t < cutoff:
                continue
            out.append(mk(name, it["id"], t, it.get("title", ""), it.get("content_text") or it.get("content", ""),
                          it.get("uri", ""), flag=(it.get("score") or 0) >= 2))
        if dt.datetime.fromtimestamp(items[-1]["display_time"], BJ) < cutoff:
            break
        cursor = d.get("next_cursor") or ""
        if not cursor:
            break
        time.sleep(0.5)
    return out


def src_eastmoney(cutoff):
    out, sort_end = [], ""
    for _ in range(15):
        js = json.loads(http("https://np-weblist.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724"
                             f"&fastColumn=102&sortEnd={sort_end}&pageSize=200&req_trace={int(time.time())}"))
        lst = (js.get("data") or {}).get("fastNewsList") or []
        if not lst:
            break
        for it in lst:
            t = dt.datetime.strptime(it["showTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ)
            if t < cutoff:
                continue
            out.append(mk("东方财富7x24", it["code"], t, it.get("title", ""), it.get("summary", ""),
                          f"https://finance.eastmoney.com/a/{it['code']}.html", flag=it.get("titleColor") == 3))
        last = lst[-1]
        if dt.datetime.strptime(last["showTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ) < cutoff:
            break
        sort_end = last.get("realSort", "")
        if not sort_end:
            break
        time.sleep(0.5)
    return out


# ---------------- 官方发布 ----------------

def src_gov_json(cutoff):
    out = []
    for it in json.loads(http("https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json")):
        t = dt.datetime.strptime(it["DOCRELPUBTIME"][:10], "%Y-%m-%d").replace(tzinfo=BJ)
        if t.date() < cutoff.date():
            continue
        sid = hashlib.md5(it["URL"].encode()).hexdigest()[:12]
        out.append(mk("国务院", sid, t, it["TITLE"], it.get("SUB_TITLE", ""), it["URL"], level="一级", kind="政策", flag=True))
    return out


DATE_IN_URL = [re.compile(r"t(20\d{2})(\d{2})(\d{2})_"), re.compile(r"/(20\d{2})(\d{2})/"),
               re.compile(r"(20\d{2})-(\d{2})-(\d{2})")]
DATE_NEAR = re.compile(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})")


def src_html_list(cutoff, name, url):
    """通用列表页：取正文里带日期的链接。日期优先从链接本身取，取不到就看链接后 200 个字符。"""
    page = http(url)
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S):
        href, inner = m.group(1), m.group(2)
        title = strip_html(re.search(r'title="([^"]+)"', m.group(0)).group(1)) if 'title="' in m.group(0) else strip_html(inner)
        if len(title) < 8 or href.startswith("javascript"):
            continue
        full = urllib.parse.urljoin(url, href)
        y = mo = d = None
        for pat in DATE_IN_URL:
            mm = pat.search(href)
            if mm and len(mm.groups()) == 3:
                y, mo, d = mm.groups()
                break
        if y is None:
            mm = DATE_NEAR.search(page[m.end(): m.end() + 200])
            if not mm:
                continue
            y, mo, d = mm.groups()
        try:
            t = dt.datetime(int(y), int(mo), int(d), tzinfo=BJ)
        except ValueError:
            continue
        if t.date() < cutoff.date() or full in seen:
            continue
        seen.add(full)
        out.append(mk(name, hashlib.md5(full.encode()).hexdigest()[:12], t, title, "", full, level="一级", kind="政策", flag=True))
    return out


OFFICIAL = {
    "发改委": "https://www.ndrc.gov.cn/xwdt/xwfb/",
    "统计局": "https://www.stats.gov.cn/sj/zxfb/",
    "人民银行": "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
    "证监会": "http://www.csrc.gov.cn/csrc/c100028/common_list.shtml",
}


# ---------------- 股票池公告（巨潮） ----------------

ANN_KEY = ["立案", "处罚", "问询函", "关注函", "监管函", "减持", "增持", "回购", "业绩预告", "业绩快报", "权益分派",
           "利润分配", "分红", "重大合同", "中标", "停牌", "复牌", "诉讼", "质押", "非标", "风险警示", "重大资产",
           "年度报告", "半年度报告", "季度报告", "董事长", "辞职", "收购", "定增", "向特定对象发行"]


def cninfo_orgids(codes):
    path = os.path.join(NEWS, "巨潮机构代码.json")
    cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    changed = False
    for c in codes:
        if c in cache:
            continue
        try:
            res = json.loads(http("http://www.cninfo.com.cn/new/information/topSearch/query",
                                  data={"keyWord": c, "maxNum": "5"}))
            hit = next((x for x in res if x.get("code") == c), None)
            if hit:
                cache[c] = hit["orgId"]
                changed = True
        except Exception:
            pass
        time.sleep(0.3)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(cache.items())), f, ensure_ascii=False, indent=1)
    return cache


def src_cninfo(cutoff, pool):
    codes = sorted(pool)
    orgs = cninfo_orgids(codes)
    for c in codes:  # 取不到 orgId 的按惯例猜
        orgs.setdefault(c, ("gssh0" if c.startswith("6") else "gssz0") + c)
    out = []
    se = f"{cutoff.strftime('%Y-%m-%d')}~{now_bj().strftime('%Y-%m-%d')}"
    for i in range(0, len(codes), 20):
        stock = ";".join(f"{c},{orgs[c]}" for c in codes[i:i + 20])
        for page in range(1, 20):
            js = json.loads(http("http://www.cninfo.com.cn/new/hisAnnouncement/query", data={
                "pageNum": str(page), "pageSize": "30", "column": "szse", "tabName": "fulltext", "plate": "",
                "stock": stock, "searchkey": "", "secid": "", "category": "", "trade": "", "seDate": se,
                "sortName": "", "sortType": "", "isHLtitle": "true"}))
            anns = js.get("announcements") or []
            for a in anns:
                if a.get("secCode") not in pool:
                    continue
                t = dt.datetime.fromtimestamp(a["announcementTime"] / 1000, BJ)
                if t < cutoff:
                    continue
                title = strip_html(a.get("announcementTitle", ""))
                name = pool[a["secCode"]]
                r = mk("巨潮公告", a["announcementId"], t, f"{name}：{title}", "",
                       "http://static.cninfo.com.cn/" + a.get("adjunctUrl", ""), level="一级", kind="公告",
                       flag=any(k in title for k in ANN_KEY))
                r["code"] = a["secCode"]
                out.append(r)
            if not js.get("hasMore") or len(anns) < 30:
                break
            time.sleep(0.4)
        time.sleep(0.4)
    return out


# ---------------- 归类与打分 ----------------

def load_pool():
    pool = {}
    if os.path.exists(POOL):
        with open(POOL, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                pool[r["code"]] = r["name"]
    return pool


def tagger(rules, pool):
    groups = {g: rules.get(g, {}) for g in ("宏观", "驱动", "行业")}
    imp = rules.get("重要词", [])

    def tag(rec):
        s = rec["title"] + " " + rec["text"]
        tags = {g: [k for k, kws in d.items() if any(w in s for w in kws)] for g, d in groups.items()}
        stocks = sorted({c for c, n in pool.items() if n and n in s})
        if rec.get("code"):
            stocks = sorted(set(stocks) | {rec["code"]})
        tags["个股"] = stocks
        score = 1 + (any(w in s for w in imp)) + bool(tags["驱动"] or stocks) + bool(rec["flag"])
        rec["tags"] = {k: v for k, v in tags.items() if v}
        rec["score"] = min(3, score)
        return rec
    return tag


def norm_key(rec):
    s = re.sub(r"[\W_]+", "", rec["title"] + rec["text"])[:40]
    return hashlib.md5(s.encode()).hexdigest()


def day_file(date):
    return os.path.join(NEWS, date[:7], date + ".jsonl")


def load_recent(days=3):
    recs = []
    for i in range(days):
        p = day_file((now_bj() - dt.timedelta(days=i)).strftime("%Y-%m-%d"))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                recs += [json.loads(x) for x in f if x.strip()]
    return recs


def write_digest(recent, status):
    cut = (now_bj() - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    day = [r for r in recent if r["time"] >= cut and not r.get("dup_of")]
    day.sort(key=lambda r: (-r["score"], r["time"]), reverse=False)
    cnt = {}
    for r in day:
        for g, vs in r.get("tags", {}).items():
            if g == "个股":
                continue
            for v in vs:
                cnt[(g, v)] = cnt.get((g, v), 0) + 1
    L = [f"# 新闻库 · 最近 24 小时", "", f"更新：{now_bj().strftime('%Y-%m-%d %H:%M')}（北京时间）；共 {len(day)} 条，"
         f"重要度3：{sum(r['score'] == 3 for r in day)} 条，重要度2：{sum(r['score'] == 2 for r in day)} 条。", "",
         "归类是程序按关键词初分的，Claude 复核时以原文为准。", "", "## 标签计数", ""]
    for g in ("宏观", "驱动", "行业"):
        items = sorted(((v, n) for (gg, v), n in cnt.items() if gg == g), key=lambda x: -x[1])
        if items:
            L.append(f"- {g}：" + "，".join(f"{v} {n}" for v, n in items))
    for sc in (3, 2):
        sel = sorted((r for r in day if r["score"] == sc), key=lambda r: r["time"], reverse=True)
        L += ["", f"## 重要度 {sc}（{len(sel)} 条）", ""]
        for r in sel[:80 if sc == 3 else 60]:
            tg = "；".join(f"{'/'.join(v)}" for k, v in r.get("tags", {}).items() if k != "个股")
            st = r.get("tags", {}).get("个股")
            L.append(f"- {r['time'][5:]} [{r['source']}] {r['title'][:90]}" + (f"　〔{tg}〕" if tg else "") +
                     (f"　〔个股 {','.join(st)}〕" if st else ""))
    L += ["", "## 来源状态", ""]
    for k, v in status.items():
        L.append(f"- {k}：" + (f"{v['n']} 条" if v["ok"] else f"失败 {v['err']}"))
    with open(os.path.join(NEWS, "最新.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=36, help="往回抓多少小时（已入库的会自动跳过）")
    a = ap.parse_args()
    os.makedirs(NEWS, exist_ok=True)
    cutoff = now_bj() - dt.timedelta(hours=a.hours)
    rules = json.load(open(RULES, encoding="utf-8"))
    pool = load_pool()
    tag = tagger(rules, pool)

    jobs = [("新浪7x24", lambda: src_sina(cutoff)),
            ("华尔街见闻A股", lambda: src_wscn(cutoff, "a-stock-channel", "华尔街见闻")),
            ("华尔街见闻全球", lambda: src_wscn(cutoff, "global-channel", "华尔街见闻")),
            ("东方财富7x24", lambda: src_eastmoney(cutoff)),
            ("国务院", lambda: src_gov_json(cutoff))]
    jobs += [(k, (lambda k=k, u=u: src_html_list(cutoff, k, u))) for k, u in OFFICIAL.items()]
    jobs.append(("巨潮公告", lambda: src_cninfo(cutoff, pool)))

    status, fetched = {}, []
    for name, fn in jobs:
        try:
            got = fn()
            status[name] = {"ok": True, "n": len(got)}
            fetched += got
        except Exception as e:
            status[name] = {"ok": False, "n": 0, "err": f"{type(e).__name__}: {e}"[:150]}
        print(name, status[name])

    recent = load_recent(4)
    seen_id = {r["id"] for r in recent}
    seen_key = {norm_key(r): r["id"] for r in recent}
    new = []
    for r in sorted(fetched, key=lambda r: r["time"]):
        if r["id"] in seen_id:
            continue
        seen_id.add(r["id"])
        k = norm_key(r)
        if r["kind"] == "快讯" and k in seen_key:
            r["dup_of"] = seen_key[k]
        else:
            seen_key[k] = r["id"]
        new.append(tag(r))

    by_day = {}
    for r in new:
        by_day.setdefault(r["date"], []).append(r)
    for d, rs in by_day.items():
        p = day_file(d)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for r in rs:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    write_digest(recent + new, status)
    with open(os.path.join(NEWS, "状态.json"), "w", encoding="utf-8") as f:
        json.dump({"最近运行": now_bj().strftime("%Y-%m-%d %H:%M"), "本次新增": len(new),
                   "其中重复转载": sum(1 for r in new if r.get("dup_of")), "来源": status},
                  f, ensure_ascii=False, indent=2)
    print(f"新增 {len(new)} 条（重复转载 {sum(1 for r in new if r.get('dup_of'))}）")
    # 快讯源全挂才算失败
    if not any(status[k]["ok"] for k in ("新浪7x24", "华尔街见闻A股", "东方财富7x24")):
        sys.exit(1)


if __name__ == "__main__":
    main()
