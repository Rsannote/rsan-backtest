#!/usr/bin/env python3
"""信息源监控 · 抓取今日热榜（tophub）上选中的榜单。

榜单名单在 system/信息源白名单.json 的 "tophub" 部分。每天 4 次由
.github/workflows/info-monitor.yml 在 GitHub 服务器上运行，也可以手动：
    python3 scripts/fetch_tophub.py [--no-body]

产出（只追加，不改写）：
- data/信息源/tophub/<年-月>/<日期>.jsonl   每条“第一次见到的条目”一行：首见时间、榜单、层、目的、排名、标题、热度、链接
- data/信息源/tophub/正文/<日期>.jsonl      “正文=true”榜单的新条目，去原网站抓的正文节选（最多 2000 字）
- data/信息源/tophub/已见.json              每个榜单最近见过的条目 ID，用来去重
- data/信息源/tophub/状态.json              每个榜单最近一次有新条目的时间、连续多少次没有新条目（判断死榜）
- data/信息源/tophub/运行记录.txt           每次运行每个榜单拿到多少条、新多少条、失败原因
每个榜单单独容错；请求间隔 2 秒。
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = os.path.join("data", "信息源", "tophub")
BODY_MAX_PER_RUN = 15
BODY_CHARS = 2000
SEEN_KEEP = 600

now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
stamp = now.strftime("%Y-%m-%d %H:%M")
today = now.strftime("%Y-%m-%d")


def fetch(url, referer=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer or url,
                                               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
    time.sleep(2)
    m = re.search(r"charset=([\w-]+)", ctype, re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:3000], re.I)
    enc = m.group(1) if m else "utf-8"
    if isinstance(enc, bytes):
        enc = enc.decode()
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_node(page):
    """榜单页是一张表，一行一条。两种行：纯文字行（标题+热度列）和带缩略图的行（标题下有作者或摘要、播放量）。"""
    out = []
    for tr in re.findall(r"(?s)<tr>(.*?)</tr>", page):
        rk = re.search(r'<td align="center">(\d+)\.</td>', tr)
        a = re.search(r'<a href="([^"]*)"[^>]*itemid="(\d+)"[^>]*>(.*?)</a>', tr, re.S)
        if not rk or not a or not clean(a.group(3)):
            continue
        heat = re.search(r'<td class="ws">(.*?)</td>', tr, re.S)
        extra = re.search(r'class="item-extra">(.*?)</div>', tr, re.S)
        desc = re.search(r'class="item-desc">(.*?)</div>', tr, re.S)
        row = {"排名": int(rk.group(1)), "链接": html.unescape(a.group(1)), "itemid": a.group(2),
               "标题": clean(a.group(3)),
               "热度": clean(heat.group(1)) if heat else clean(extra.group(1)) if extra else ""}
        if desc and clean(desc.group(1)):
            row["描述"] = clean(desc.group(1))[:80]
        out.append(row)
    return out


def body_text(url):
    page = fetch(url)
    page = re.sub(r"(?is)<(script|style|noscript|header|footer|nav)[^>]*>.*?</\1>", " ", page)
    paras = [clean(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page)]
    paras = [p for p in paras if len(p) >= 15]
    text = "\n".join(paras)
    if len(text) < 200:
        m = re.search(r'(?is)<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']', page)
        if m:
            text = (clean(m.group(1)) + "\n" + text).strip()
    return text[:BODY_CHARS]


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-body", action="store_true", help="不抓正文")
    args = ap.parse_args()

    nodes = json.load(open("system/信息源白名单.json", encoding="utf-8"))["tophub"]["榜单"]
    seen = load(os.path.join(BASE, "已见.json"), {})
    state = load(os.path.join(BASE, "状态.json"), {})
    out_path = os.path.join(BASE, now.strftime("%Y-%m"), today + ".jsonl")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    log, new_rows, body_queue = [f"## {stamp}"], [], []
    for n in nodes:
        nid = n["节点ID"]
        name = f"{n['网站']}·{n['榜单']}".rstrip("·")
        try:
            items = parse_node(fetch(f"https://tophub.today/n/{nid}", "https://tophub.today/"))
        except Exception as e:  # 单个榜单失败不影响其他
            log.append(f"{name}\t失败\t{type(e).__name__}: {e}")
            continue
        old = seen.get(nid, [])
        old_set = set(old)
        fresh = [it for it in items if it["itemid"] not in old_set]
        first_run = nid not in seen
        for it in fresh:
            row = {"首见": stamp, "节点ID": nid, "网站": n["网站"], "榜单": n["榜单"], "层": n["层"],
                   "目的": n["目的"], **it}
            if first_run:
                row["首次运行"] = True   # 首次抓到的存量条目，不代表刚冒头
            new_rows.append(row)
            if n.get("正文") and not first_run:
                body_queue.append(row)
        seen[nid] = ([it["itemid"] for it in fresh] + old)[:SEEN_KEEP]
        st = state.setdefault(nid, {"榜单": name})
        st["最近抓取"] = stamp
        if fresh:
            st["最近有新条目"] = stamp
            st["连续无新"] = 0
        else:
            st["连续无新"] = st.get("连续无新", 0) + 1
        if not items:
            log.append(f"{name}\t0条\t页面没有解析出条目")
        else:
            log.append(f"{name}\t{len(items)}条\t新{len(fresh)}")

    with open(out_path, "a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if not args.no_body and body_queue:
        body_queue.sort(key=lambda r: r["排名"])
        bpath = os.path.join(BASE, "正文", today + ".jsonl")
        os.makedirs(os.path.dirname(bpath), exist_ok=True)
        ok = 0
        with open(bpath, "a", encoding="utf-8") as f:
            for r in body_queue[:BODY_MAX_PER_RUN]:
                try:
                    txt = body_text(r["链接"])
                except Exception as e:
                    txt, err = "", f"{type(e).__name__}"
                else:
                    err = "" if len(txt) >= 100 else "正文过短"
                if txt:
                    ok += 1
                f.write(json.dumps({"首见": r["首见"], "节点ID": r["节点ID"], "网站": r["网站"], "itemid": r["itemid"],
                                    "标题": r["标题"], "链接": r["链接"], "正文": txt, "备注": err},
                                   ensure_ascii=False) + "\n")
        log.append(f"正文\t尝试{min(len(body_queue), BODY_MAX_PER_RUN)}\t成功{ok}")

    save(os.path.join(BASE, "已见.json"), seen)
    save(os.path.join(BASE, "状态.json"), state)
    with open(os.path.join(BASE, "运行记录.txt"), "a", encoding="utf-8") as f:
        f.write("\n".join(log) + f"\n新条目合计 {len(new_rows)}\n\n")
    print("\n".join(log))
    print("新条目合计", len(new_rows))


if __name__ == "__main__":
    main()
