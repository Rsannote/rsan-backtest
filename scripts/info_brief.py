#!/usr/bin/env python3
"""信息源监控 · 每日简报（程序预筛，给 Claude 和 Rsan 读）。

读 data/信息源/tophub/ 里当天及前 14 天的条目，按四个目的整理成一份短简报：
  一、散户情绪：大众平台“破圈”程度（炒股理财话题在小红书/抖音/微博/知乎/B站热榜上的条数，对比 7 日均值）+ 投资社区热帖
  二、AI 用法与前沿：各 AI 源的新条目（有正文的附节选）
  三、高质量解盘：主流财经媒体、研报、海外科技财经的新条目
  四、面包屑与预期差：按“词条”（股票池公司、行业词、驱动词、同花顺热门概念）统计各层提及，
      找出“社区/大众先热、媒体和快讯还没跟”的词条和“热度跃升”的词条
用法：python3 scripts/info_brief.py [--date YYYY-MM-DD]
产出：data/信息源/简报/<日期>.md 和 data/信息源/简报/最新.md
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import json
import os
import re

BASE = os.path.join("data", "信息源", "tophub")
OUT = os.path.join("data", "信息源", "简报")
FIN_WORDS = ["股票", "A股", "a股", "炒股", "股市", "牛市", "熊市", "大盘", "基金", "涨停", "跌停", "理财", "开户",
             "券商", "股民", "上证", "沪指", "创业板", "科创板", "抄底", "割肉", "套牢", "黄金", "金价", "美股",
             "港股", "纳指", "降息", "降准", "存款利率", "ETF", "爆仓", "赚钱", "亏钱"]
COMMUNITY_LAYERS = {"线索", "大众"}
MEDIA_LAYERS = {"媒体", "事实"}


def read_days(day, n):
    """返回 {日期: [条目...]}，包括 day 及之前 n 天。"""
    out = {}
    for i in range(n + 1):
        d = (day - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        rows = []
        for p in glob.glob(os.path.join(BASE, "*", d + ".jsonl")):
            with open(p, encoding="utf-8") as f:
                rows += [json.loads(l) for l in f if l.strip()]
        out[d] = rows
    return out


def read_bodies(d):
    p = os.path.join(BASE, "正文", d + ".jsonl")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return {(r["节点ID"], r["itemid"]): r for r in map(json.loads, f) if r.get("正文")}


def build_terms():
    """词条 -> 类别。股票池名称、行业词、驱动词、最近一次同花顺热门概念。"""
    terms = {}
    try:
        with open("data/股票池/股票.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("name") and len(r["name"]) >= 2:
                    terms[r["name"]] = f"股票池·{r.get('sector', '')}"
    except OSError:
        pass
    rules = json.load(open("system/新闻归类.json", encoding="utf-8"))
    for ind, words in rules.get("行业", {}).items():
        for w in words:
            terms.setdefault(w, f"行业·{ind}")
    for drv, words in rules.get("驱动", {}).items():
        for w in words:
            terms.setdefault(w, f"驱动·{drv}")
    files = sorted(glob.glob("data/社区情绪/*/同花顺热门概念.csv"))
    if files:
        with open(files[-1], encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                name = r.get("name", "")
                for part in re.split(r"[()（）]", name):
                    part = part.replace("概念", "").strip()
                    if len(part) >= 2:
                        terms.setdefault(part, "热门概念")
    # 太泛、容易误报的词去掉
    for w in ["电力", "红利", "装机", "风电", "航空", "铁路", "港口", "快递", "粮食", "银行", "保险"]:
        if terms.get(w, "").startswith(("行业", "驱动")):
            terms.pop(w)
    return terms


def hits(title, terms):
    return [t for t in terms if t in title]


def line(r, body=None, n=110):
    s = f"- [{r['标题'][:80]}]({r['链接']})"
    if r.get("热度"):
        s += f"（{r['热度']}）"
    if body:
        txt = body["正文"].replace("\n", " ")[:n]
        s += f"\n  > {txt}…"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args()
    tz = dt.timezone(dt.timedelta(hours=8))
    day = dt.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else dt.datetime.now(tz).date()
    ds = day.strftime("%Y-%m-%d")
    days = read_days(day, 14)
    rows = days[ds]
    bodies = read_bodies(ds)
    nodes = json.load(open("system/信息源白名单.json", encoding="utf-8"))["tophub"]["榜单"]
    state = json.load(open(os.path.join(BASE, "状态.json"), encoding="utf-8")) if os.path.exists(os.path.join(BASE, "状态.json")) else {}

    hist_days = [d for d in days if d != ds and days[d]]
    md = [f"# 信息源简报 {ds}",
          "",
          f"今日新条目 {len(rows)} 条，来自 {len({r['节点ID'] for r in rows})} 个榜单；历史可比天数 {len(hist_days)} 天。"
          "全部是三级信息源，只当线索。"]
    if any(r.get("首次运行") for r in rows):
        md.append("（含首次运行抓到的存量条目；“先冒头”的判断需要攒满一周历史后才可靠。）")

    # 一、散户情绪：破圈
    def fin_count(rs):
        return [r for r in rs if r["层"] == "大众" and any(w in r["标题"] for w in FIN_WORDS)]
    today_fin = fin_count(rows)
    hist_counts = [len(fin_count(days[d])) for d in hist_days[:7]]
    avg = sum(hist_counts) / len(hist_counts) if hist_counts else None
    md += ["", "## 一、散户情绪", "",
           f"**破圈指标**：大众平台热榜里炒股理财相关条目 {len(today_fin)} 条"
           + (f"，前 {len(hist_counts)} 天均值 {avg:.1f}" if avg is not None else "，暂无历史均值") + "。"
           "条数明显高于均值、且出现在小红书/抖音/微博这类非财经平台时，通常说明情绪在升温。"]
    by_site = collections.Counter(r["网站"] for r in today_fin)
    if today_fin:
        md.append("分布：" + "、".join(f"{k} {v}" for k, v in by_site.most_common()))
        md += [line(r) for r in sorted(today_fin, key=lambda r: r["排名"])[:8]]
    md += ["", "**投资社区热帖**（淘股吧、雪球、集思录、东财社区）："]
    comm = [r for r in rows if r["网站"] in ("淘股吧", "雪球", "集思录", "东方财富网") and 1 in r["目的"]]
    md += [line(r) for r in sorted(comm, key=lambda r: r["排名"])[:12]] or ["- 今日无新条目"]

    # 二、AI
    md += ["", "## 二、AI 用法与前沿", ""]
    ai_nodes = [n for n in nodes if 2 in n["目的"]]
    for n in ai_nodes:
        rs = sorted([r for r in rows if r["节点ID"] == n["节点ID"]], key=lambda r: r["排名"])[:5]
        if rs:
            md.append(f"**{n['网站']}{('·' + n['榜单']) if n['榜单'] else ''}**")
            md += [line(r, bodies.get((r["节点ID"], r["itemid"]))) for r in rs]

    # 三、高质量解盘
    md += ["", "## 三、高质量解盘", ""]
    for n in [n for n in nodes if 3 in n["目的"] and n["层"] in ("媒体", "事实")]:
        k = 3 if n["网站"] == "财联社" else 5
        rs = sorted([r for r in rows if r["节点ID"] == n["节点ID"]], key=lambda r: r["排名"])[:k]
        if rs:
            md.append(f"**{n['网站']}·{n['榜单']}**".replace("·**", "**"))
            md += [line(r, bodies.get((r["节点ID"], r["itemid"]))) for r in rs]

    # 四、面包屑
    terms = build_terms()
    stat = collections.defaultdict(lambda: {"今日社区": 0, "今日媒体": 0, "近3日媒体": 0, "近7日日均": 0.0,
                                            "例": [], "首见社区": None, "首见媒体": None})
    for d, rs in days.items():
        age = (day - dt.datetime.strptime(d, "%Y-%m-%d").date()).days
        for r in rs:
            layer_c = r["层"] in COMMUNITY_LAYERS
            layer_m = r["层"] in MEDIA_LAYERS
            for t in hits(r["标题"], terms):
                s = stat[t]
                if age == 0:
                    if layer_c:
                        s["今日社区"] += 1
                        if len(s["例"]) < 2:
                            s["例"].append(r)
                    if layer_m:
                        s["今日媒体"] += 1
                if age <= 3 and layer_m:
                    s["近3日媒体"] += 1
                if 1 <= age <= 7:
                    s["近7日日均"] += 1 / 7
                key = "首见社区" if layer_c else "首见媒体" if layer_m else None
                if key and not r.get("首次运行"):
                    if s[key] is None or r["首见"] < s[key]:
                        s[key] = r["首见"]
    lead = [(t, s) for t, s in stat.items() if s["今日社区"] >= 2 and s["近3日媒体"] == 0]
    lead.sort(key=lambda x: -x[1]["今日社区"])
    jump = [(t, s) for t, s in stat.items()
            if s["今日社区"] + s["今日媒体"] >= 3 and len(hist_days) >= 3
            and (s["今日社区"] + s["今日媒体"]) >= 3 * max(s["近7日日均"], 0.5)]
    jump.sort(key=lambda x: -(x[1]["今日社区"] + x[1]["今日媒体"]))
    md += ["", "## 四、面包屑与预期差", "",
           "词条来自股票池公司、行业词、驱动词和同花顺热门概念。程序只做初筛，是否构成预期差由 Claude 结合新闻库和股价判断。", "",
           "**社区先热、媒体未跟**（今日社区/大众层提及≥2，近 3 天媒体和快讯层 0 次）："]
    if lead:
        for t, s in lead[:12]:
            md.append(f"- **{t}**（{terms[t]}）社区 {s['今日社区']} 次｜例：" + "；".join(f"[{r['标题'][:40]}]({r['链接']})" for r in s["例"]))
    else:
        md.append("- 今日没有")
    md += ["", "**热度跃升**（今日提及≥3，且是前 7 天日均的 3 倍以上；历史不足 3 天时不计算）："]
    if jump:
        for t, s in jump[:12]:
            md.append(f"- **{t}**（{terms[t]}）今日 社区{s['今日社区']} 媒体{s['今日媒体']}｜前7日日均 {s['近7日日均']:.1f}")
    else:
        md.append("- 今日没有" + ("（历史不足）" if len(hist_days) < 3 else ""))
    pool_fact = [r for r in rows if r["层"] == "事实" and any(terms.get(t, "").startswith("股票池") for t in hits(r["标题"], terms))]
    md += ["", "**股票池公司的快讯和公告**："]
    md += [line(r) for r in pool_fact[:15]] or ["- 今日没有"]

    # 榜单健康
    dead = [s["榜单"] for s in state.values() if s.get("连续无新", 0) >= 12]
    if dead:
        md += ["", "## 榜单健康", "", "连续 12 次（约 3 天）没有新条目，可能已停更：" + "、".join(dead)]

    os.makedirs(OUT, exist_ok=True)
    text = "\n".join(md) + "\n"
    for p in (os.path.join(OUT, ds + ".md"), os.path.join(OUT, "最新.md")):
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"简报已写入 {OUT}/{ds}.md，{len(text)} 字")


if __name__ == "__main__":
    main()
