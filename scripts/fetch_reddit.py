#!/usr/bin/env python3
"""信息源监控 · Reddit 版块（T10）。

版块名单在 system/信息源白名单.json 的「平台.Reddit.条目」。每个版块取公开 RSS
`https://www.reddit.com/r/<版块>/top/.rss?t=week`（本周最热），版块之间间隔约 60 秒
（2026-10-22 探测：连续请求会被 429 限流；JSON 接口 403、old.reddit 返回验证页）。

产出（只追加，不改写）：
- data/信息源/reddit/<年-月>/<日期>.jsonl   每条“第一次见到的帖子”一行：首见时间、版块、目的、本周排名、标题、作者、发帖时间、链接、正文节选
- data/信息源/reddit/已见.json              每个版块最近见过的帖子 ID，用来去重
- data/信息源/reddit/状态.json              每个版块最近一次成功、连续失败次数、最近错误；顶层 last_success_at
- data/信息源/reddit/运行记录.txt           每次运行每个版块拿到多少条、新多少条、失败原因（追加）
- data/信息源/精华_最新.md                  按白名单「目的」分组，每个目的最多 10 条（本次新帖按本周排名），供 Claude 阅读

全部属于三级信息源，只当线索。每个版块单独容错；只用标准库。
用法：python3 scripts/fetch_reddit.py [--gap 60] [--root 仓库根] [--out-dir 隔离输出目录]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BJ = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ATOM = "{http://www.w3.org/2005/Atom}"
SEEN_KEEP = 300
SNIPPET = 300
PURPOSES = {1: "散户情绪", 2: "AI用法、技能、提示词与前沿", 3: "宏观/产业/A股/美股高质量解盘", 4: "面包屑、预期差、新机会"}
PER_PURPOSE = 10


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def subreddits(root: Path):
    wl = load_json(root / "system/信息源白名单.json", {})
    items = ((wl.get("平台") or {}).get("Reddit") or {}).get("条目") or []
    out = []
    for it in items:
        m = re.fullmatch(r"r/([A-Za-z0-9_]+)", it.get("地址", ""))
        if m and it.get("状态") != "抓不到":
            out.append({"名称": it["名称"], "版块": m.group(1), "目的": it.get("目的", []), "备注": it.get("备注", "")})
    return out


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"submitted by /u/\S+ \[link\] \[comments\]$", "", text).strip()
    return text[:SNIPPET]


def parse_feed(body: bytes):
    """Reddit Atom → [{id,title,link,author,published,snippet,rank}]；不是 Atom 就报错。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"不是合法 XML（可能是验证页）：{e}") from None
    if root.tag != ATOM + "feed":
        raise ValueError(f"不是 Atom feed：{root.tag[:60]}")
    posts = []
    for rank, e in enumerate(root.findall(ATOM + "entry"), 1):
        link = e.find(ATOM + "link")
        author = e.find(f"{ATOM}author/{ATOM}name")
        posts.append({"id": (e.findtext(ATOM + "id") or "").strip(),
                      "title": html.unescape((e.findtext(ATOM + "title") or "").strip()),
                      "link": link.get("href") if link is not None else "",
                      "author": (author.text or "").strip() if author is not None else "",
                      "published": (e.findtext(ATOM + "published") or e.findtext(ATOM + "updated") or "").strip(),
                      "snippet": strip_html(e.findtext(ATOM + "content") or ""), "rank": rank})
    return [p for p in posts if p["id"] and p["title"]]


def fetch(sub: str, retry_wait: int):
    url = f"https://www.reddit.com/r/{sub}/top/.rss?t=week"
    last = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
                                                   "Accept-Language": "en-US,en;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return parse_feed(r.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code != 429 or attempt:
                break
            time.sleep(retry_wait)              # 被限流：等一会儿只重试一次
        except Exception as e:  # noqa: BLE001  每个版块单独容错
            last = f"{type(e).__name__}: {str(e)[:120]}"
            break
    raise RuntimeError(last or "未知错误")


def digest(new_rows, now: dt.datetime, status) -> str:
    lines = ["# 信息源精华（Reddit）", "",
             f"生成：{now.strftime('%Y-%m-%d %H:%M')}（北京时间）。程序按白名单「目的」预筛，每个目的最多 {PER_PURPOSE} 条本次新帖，按版块本周排名排序。",
             "**全部是三级信息源，只当线索，不能单独支撑判断。**", ""]
    for k, name in PURPOSES.items():
        rows = sorted((r for r in new_rows if k in r["目的"]), key=lambda r: (r["本周排名"], r["版块"]))[:PER_PURPOSE]
        if not rows:
            continue
        lines += [f"## 目的{k}：{name}", ""]
        for r in rows:
            lines.append(f"- [{r['标题']}]({r['链接']})（r/{r['版块']}，本周第 {r['本周排名']}）")
            if r["节选"]:
                lines.append(f"  {r['节选'][:160]}")
        lines.append("")
    if not new_rows:
        lines += ["本次没有新帖。", ""]
    failed = [f"r/{k}（{v.get('最近错误')}）" for k, v in status.items() if isinstance(v, dict) and v.get("连续失败", 0)]
    if failed:
        lines += ["本次失败的版块：" + "、".join(failed), ""]
    return "\n".join(lines)


def run(root: Path, out: Path, gap: float, now: dt.datetime | None = None, fetcher=fetch) -> int:
    now = now or dt.datetime.now(BJ)
    stamp, today = now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d")
    base = out / "data/信息源/reddit"
    base.mkdir(parents=True, exist_ok=True)
    seen = load_json(base / "已见.json", {})
    status = load_json(base / "状态.json", {})
    subs = subreddits(root)
    if not subs:
        raise SystemExit("白名单里没有可抓的 Reddit 版块")
    new_rows, log, ok = [], [f"== {stamp} =="], 0
    for i, s in enumerate(subs):
        if i:
            time.sleep(gap)
        st = status.setdefault(s["版块"], {})
        try:
            posts = fetcher(s["版块"], max(60, int(gap * 2)))
            ok += 1
            old = set(seen.get(s["版块"], []))
            fresh = [p for p in posts if p["id"] not in old]
            for p in fresh:
                new_rows.append({"首见": stamp, "版块": s["版块"], "目的": s["目的"], "本周排名": p["rank"], "标题": p["title"],
                                 "作者": p["author"], "发帖时间": p["published"], "链接": p["link"], "节选": p["snippet"],
                                 "id": p["id"], "来源级别": "三级"})
            seen[s["版块"]] = ([p["id"] for p in posts] + [x for x in seen.get(s["版块"], []) if x not in {p["id"] for p in posts}])[:SEEN_KEEP]
            st.update({"最近成功": stamp, "连续失败": 0, "最近条数": len(posts), "最近新条目": len(fresh)})
            st.pop("最近错误", None)
            log.append(f"r/{s['版块']}：{len(posts)} 条，新 {len(fresh)} 条")
        except Exception as e:  # noqa: BLE001
            st.update({"最近错误": str(e)[:160], "连续失败": st.get("连续失败", 0) + 1, "最近尝试": stamp})
            log.append(f"r/{s['版块']}：失败 {str(e)[:160]}")
        print(log[-1], flush=True)
    if new_rows:
        month = base / now.strftime("%Y-%m")
        month.mkdir(exist_ok=True)
        with (month / f"{today}.jsonl").open("a", encoding="utf-8") as fh:
            for r in new_rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    status["_汇总"] = {"最近运行": stamp, "成功版块": ok, "版块数": len(subs), "新帖": len(new_rows)}
    if ok:
        status["last_success_at"] = now.isoformat()
    (base / "已见.json").write_text(json.dumps(seen, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    (base / "状态.json").write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    with (base / "运行记录.txt").open("a", encoding="utf-8") as fh:
        fh.write("\n".join(log + [f"成功 {ok}/{len(subs)}，新帖 {len(new_rows)}", ""]) + "\n")
    (out / "data/信息源/精华_最新.md").write_text(digest(new_rows, now, {k: v for k, v in status.items() if not k.startswith("_")}),
                                              encoding="utf-8")
    print(f"成功 {ok}/{len(subs)}，新帖 {len(new_rows)}")
    return 0 if ok else 1                       # 全部失败才算失败；部分失败写在状态里


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gap", type=float, default=60, help="版块之间间隔秒数（默认60，太快会被429）")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--out-dir", type=Path, help="隔离输出根目录（测试用），默认写仓库")
    a = ap.parse_args(argv)
    return run(a.root.resolve(), (a.out_dir or a.root).resolve(), a.gap)


if __name__ == "__main__":
    sys.exit(main())
