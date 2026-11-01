#!/usr/bin/env python3
"""T5 总控 · 每周市场全景。

只读取各模块已经算好的产出，按固定栏目汇总成 `panorama/<本周最后交易日>.md`；
不重算指标、不写判断、不下结论。需要判断的地方留【待判读】，由 Claude 补写。

用法：
  python3 scripts/panorama.py --week 2026-10-23          # 手动生成某一周（该周任一日期即可）
  python3 scripts/panorama.py --week 2026-10-23 --out-dir logs/test/T5   # 隔离输出
  python3 scripts/panorama.py --week 2026-10-30 --force  # 覆盖已存在的一期（会丢掉补写的判读）
  python3 scripts/panorama.py --auto                      # 工作流用：本周数据齐了才生成，每周只写一次
  python3 scripts/panorama.py --issue                     # 工作流用：给最新一期开 Issue（标签“全景”），不重复

"本周"按北京时间自然周（周一到周日）。数据截止日 = 本周最后一个交易日。
判断档案按"本周日 24:00 之前已写入/已复核"取状态，所以任何时候重跑同一周，结果都一样。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_judgment_common import TradingCalendar, atomic_text  # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))
LABEL = "全景"
STAGES = ["启动", "确认", "拥挤", "退潮"]
ZONES = [(20, "冰点"), (40, "偏冷"), (60, "中性"), (80, "偏热"), (101, "过热")]  # 同 system/情绪规则.md
# --auto 在数据不齐时最多等到本周六这个时刻，之后照样生成，缺的栏目写明原因。
DEADLINE_WEEKDAY, DEADLINE_TIME = 5, dt.time(12, 0)
ISSUE_LOG_FIELDS = ["数据截止日", "issue", "记录时间"]
TITLE = "每周市场全景"


class NoData(Exception):
    """栏目没有可用数据；message 写原因。"""


def zone(t: float) -> str:
    for hi, name in ZONES:
        if t < hi:
            return name
    return "过热"


def rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def pct(x, digits=2) -> str:
    return "—" if x is None or pd.isna(x) else f"{x * 100:.{digits}f}%"


def pp(x, digits=2) -> str:
    """占比变化，单位百分点。"""
    return "—" if x is None or pd.isna(x) else f"{x * 100:+.{digits}f}个百分点"


def val(x) -> str:
    return "—" if x is None else str(x)


def num(x, digits=1) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:.{digits}f}"


# ---------------- 周与交易日 ----------------

class Week:
    def __init__(self, root: Path, any_day: dt.date):
        self.monday = any_day - dt.timedelta(days=any_day.weekday())
        self.sunday = self.monday + dt.timedelta(days=6)
        cal = TradingCalendar(root)
        self.days = [self.monday + dt.timedelta(days=i) for i in range(5)
                     if cal.is_open(self.monday + dt.timedelta(days=i))]
        self.last = self.days[-1] if self.days else None
        self.prev = cal.previous(self.days[0]) if self.days else None

    @property
    def label(self) -> str:
        return f"{self.monday.isoformat()} ~ {self.sunday.isoformat()}"


# ---------------- 各栏目 ----------------

def sec_sentiment(root: Path, w: Week) -> tuple[list[str], dict]:
    path = root / "data/社区情绪/温度计.csv"
    if not path.exists():
        raise NoData("缺 data/社区情绪/温度计.csv")
    t = pd.read_csv(path, encoding="utf-8-sig", dtype={"date": str})
    t = t[t["date"] <= w.last.isoformat()]
    week = t[t["date"].isin([d.isoformat() for d in w.days])]
    if week.empty:
        raise NoData(f"温度计没有本周（{w.label}）的数据")
    last = week.iloc[-1]
    temp = float(last["温度"])
    stage, stage_date, stage_src = latest_stage(root, w)
    stage_txt = f"阶段：{stage}" if stage else "阶段：暂无判读"
    basis = f"温度 {last['date']}" + (f"，阶段判读 {stage_date}" if stage else "")
    line = f"情绪温度 {temp:.0f}（{zone(temp)}）｜{stage_txt}｜{basis}"
    out = [line, ""]
    first = week.iloc[0]
    out.append(f"本周温度：{first['date'][5:]} {float(first['温度']):.0f} → {last['date'][5:]} {temp:.0f}；"
               f"分项分位（{last['date'][5:]}）：成交额 {num(last['成交额分位'])}，杠杆 {num(last['杠杆分位'])}，"
               f"赚钱效应 {num(last['赚钱效应分位'])}。")
    if stage and stage_date < w.monday.isoformat():
        out.append(f"阶段沿用 {stage_date} 的判读（本周周报尚未写入）。")
    if zone(temp) in ("冰点", "过热"):
        out.append(f"温度处在{zone(temp)}区，对当前判断意味着什么：【待判读】")
    out += ["", f"来源：`{rel(path, root)}`" + (f"、`{stage_src}`" if stage_src else "")]
    return out, {"温度": round(temp, 1), "区间": zone(temp), "阶段": stage, "阶段判读日期": stage_date, "温度日期": last["date"]}


def latest_stage(root: Path, w: Week):
    """最近一次阶段判读（周报或触发日判读），日期不晚于本周日。"""
    base = root / "data/社区情绪"
    cands = []
    for p in list(base.glob("周报/*.md")) + list(base.glob("*/情绪判读.md")):
        d = p.stem if p.parent.name == "周报" else p.parent.name
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d <= w.sunday.isoformat():
            cands.append((d, p.parent.name == "周报", p))
    for d, _, p in sorted(cands, reverse=True):
        head = p.read_text(encoding="utf-8").splitlines()[0] if p.stat().st_size else ""
        m = re.search(r"阶段[:：]\s*([^｜（(\n]+)", head)
        if m:
            return m.group(1).strip(), d, rel(p, root)
    return None, None, None


def market_day(root: Path, d: dt.date):
    p = root / "data/市场状态" / d.strftime("%Y-%m") / f"{d.isoformat()}.json"
    if not p.exists():
        return None
    return read_json(p)


def sec_market(root: Path, w: Week) -> tuple[list[str], dict]:
    rows, by_day = [], {}
    prev = market_day(root, w.prev) if w.prev else None
    prev_c = prev["市场"]["结论"] if prev else None
    changes = []
    for d in w.days:
        m = market_day(root, d)
        if not m:
            rows.append(f"|{d.isoformat()}|无存档|—|—|—|—|—|")
            continue
        s = m["市场"]
        hs = s.get("沪深300", {})
        by_day[d.isoformat()] = s["结论"]
        rows.append(f"|{d.isoformat()}|{s['结论']}|{s.get('冷热', '—')}|{s.get('总分', '—')}|"
                    f"{num(hs.get('chg'), 2)}%|{num(s.get('成交额'), 0)}|{val(s.get('涨停'))}/{val(s.get('跌停'))}|")
        if prev_c and s["结论"] != prev_c:
            changes.append(f"{d.isoformat()}：{prev_c}→{s['结论']}")
        prev_c = s["结论"]
    if not by_day:
        raise NoData(f"data/市场状态/ 下没有本周（{w.label}）任何一天的存档")
    out = ["|日期|规则结论|冷热|总分|沪深300|成交额（亿）|涨停/跌停|", "|---|---|---|---:|---:|---:|---|"] + rows + [""]
    if changes:
        out.append("本周变化：" + "；".join(changes))
    elif prev:
        out.append(f"本周变化：无（与上一交易日 {w.prev.isoformat()} 的「{prev['市场']['结论']}」相比未变）")
    else:
        out.append(f"本周变化：本周有存档的交易日之间未变（上一交易日 {w.prev.isoformat()} 无存档，无法比较）")
    last = market_day(root, w.last)
    if last:
        for line in last["市场"].get("依据", []):
            out.append(f"- {w.last.isoformat()} 依据：{line}")
        if last.get("数据告警"):
            out.append(f"- {w.last.isoformat()} 数据告警：" + "；".join(map(str, last["数据告警"])))
    out += ["", f"来源：`data/市场状态/{w.last.strftime('%Y-%m')}/<日期>.json`（规则 {last['版本'] if last else '—'}）"]
    return out, {"每日结论": by_day, "截止日结论": by_day.get(w.last.isoformat())}


def stage_table(root: Path, w: Week):
    p = root / "data/主线/阶段日表_31.csv"
    if not p.exists():
        raise NoData("缺 data/主线/阶段日表_31.csv（T15）")
    t = pd.read_csv(p, dtype={"code": str, "date": str})
    t = t[t["date"] == w.last.isoformat()].drop_duplicates(["code"], keep="last")
    if t.empty:
        raise NoData(f"阶段日表没有 {w.last.isoformat()} 的数据")
    return t, rel(p, root)


def sec_two_layer(root: Path, w: Week, market: dict | None) -> tuple[list[str], dict]:
    t, src = stage_table(root, w)
    counts = {s: int((t["阶段"] == s).sum()) for s in ["潜伏"] + STAGES}
    gate = (market or {}).get("截止日结论") or "无数据"
    out = [f"{w.last.isoformat()}：大盘规则结论 **{gate}** × 主线阶段分布 "
           + "、".join(f"{s} {counts[s]}" for s in ["潜伏"] + STAGES) + f"（共 {len(t)} 个行业）。", ""]
    ev = root / "data/事件库/当前对照.json"
    if ev.exists():
        c = read_json(ev)
        if c.get("data_date") == w.last.isoformat():
            out.append("大盘层历史对照（事件库程序相似度，距离越小越像）：")
            for r in c.get("主表排名", [])[:3]:
                out.append(f"- {r['类型']} {r['开始']}~{r.get('结束') or '—'}，距离 {num(r['距离'], 2)}")
            out += ["", "像在哪、不像在哪：【待判读】"]
        else:
            out.append(f"大盘层历史对照：暂无数据，原因：`data/事件库/当前对照.json` 只保留最新一期（{c.get('data_date')}），不是本周截止日。")
    out += ["", f"来源：`data/市场状态/`、`{src}`" + ("、`data/事件库/当前对照.json`" if ev.exists() else "")]
    return out, {"大盘": gate, "阶段分布": counts}


def flow_rows(root: Path, w: Week):
    p = root / "data/资金轮动/日表.csv"
    if not p.exists():
        raise NoData("缺 data/资金轮动/日表.csv（T8）")
    cols = ["date", "code", "name", "占比_31", "占比5日变化_31", "占比一年分位_31", "相对沪深300_5"]
    t = pd.read_csv(p, dtype={"code": str, "date": str}, usecols=cols)
    t = t[t["date"] == w.last.isoformat()].drop_duplicates(["code"], keep="last")  # 同日多条修订取最后一条
    t = t.dropna(subset=["占比5日变化_31"])
    if t.empty:
        raise NoData(f"资金轮动日表没有 {w.last.isoformat()} 的申万31口径数据")
    return t, rel(p, root)


def sec_flow(root: Path, w: Week) -> tuple[list[str], dict]:
    t, src = flow_rows(root, w)
    t = t.sort_values("占比5日变化_31", ascending=False)
    head = "|行业|成交占比|占比5日变化|5日相对沪深300|占比一年分位|"
    sep = "|---|---:|---:|---:|---:|"

    def table(df):
        return [head, sep] + [f"|{r['name']}|{pct(r['占比_31'])}|{pp(r['占比5日变化_31'])}|{pct(r['相对沪深300_5'])}|"
                              f"{num(r['占比一年分位_31'], 0)}|" for _, r in df.iterrows()]

    n = len(w.days)
    out = [f"截止 {w.last.isoformat()}，申万一级 31 口径，按「占比5日变化」排序（近 5 个交易日；本周交易日 {n} 天"
           + ("" if n == 5 else "，所以窗口跨到上周") + "）。", "", "**在追（占比上升最多的 5 个）**", ""]
    out += table(t.head(5)) + ["", "**在撤（占比下降最多的 5 个）**", ""] + table(t.tail(5).iloc[::-1])
    out += ["", f"来源：`{src}`（每个行业取该日最后一条修订）"]
    return out, {"在追": t.head(5)["name"].tolist(), "在撤": t.tail(5).iloc[::-1]["name"].tolist()}


def sec_overseas(root: Path, w: Week):
    raise NoData("T3 海外行情尚未完成，仓库里还没有海外指数日线")


def sec_mainline(root: Path, w: Week) -> tuple[list[str], dict]:
    t, src = stage_table(root, w)
    ev_path = root / "data/主线/阶段事件_31.csv"
    ev = pd.read_csv(ev_path, dtype={"code": str, "date": str}) if ev_path.exists() else pd.DataFrame(
        columns=["date", "code", "from", "to", "gate", "里程碑", "上位类型"])
    ev = ev[ev["date"] <= w.last.isoformat()]
    names = dict(zip(t["code"], t["行业"]))
    out, info = [], {}
    for s in STAGES:
        sub = t[t["阶段"] == s]
        info[s] = sub["行业"].tolist()
        if sub.empty:
            out.append(f"- **{s}**（0）：无")
            continue
        items = []
        for _, r in sub.iterrows():
            e = ev[ev["code"] == r["code"]]
            enter = e[e["to"] == s]["date"].max() if not e.empty else None
            start = e[e["里程碑"].fillna("").str.contains("启动")]
            kind = start.iloc[-1]["上位类型"] if not start.empty and pd.notna(start.iloc[-1]["上位类型"]) and start.iloc[-1]["上位类型"] else "未记录"
            items.append(f"{r['行业']}（{'自 ' + enter[5:] if isinstance(enter, str) else '进入日未知'}，上位类型 {kind}）")
        out.append(f"- **{s}**（{len(sub)}）：" + "、".join(items))
    week = ev[ev["date"] >= w.monday.isoformat()].sort_values(["date", "code"], kind="stable")
    out += ["", "**本周阶段变化**", ""]
    if week.empty:
        out.append("无")
    else:
        out += ["|日期|行业|变化|上位类型|当日大盘|", "|---|---|---|---|---|"]
        for _, r in week.iterrows():
            kind = r["上位类型"] if isinstance(r["上位类型"], str) else ""
            out.append(f"|{r['date']}|{names.get(r['code'], r['code'])}|{r['from']}→{r['to']}|{kind}|{r['gate']}|")
    params = t["参数版本"].iloc[-1] if "参数版本" in t else "—"
    out += ["", "和历史哪次最像、要盯什么：【待判读】", "",
            f"来源：`{src}`、`{rel(ev_path, root)}`（参数 {params}）"]
    info["本周变化数"] = int(len(week))
    return out, info


def sec_cross(root: Path, w: Week) -> tuple[list[str], dict]:
    p = root / "graph/打分_最新.json"
    if not p.exists():
        raise NoData("缺 graph/打分_最新.json（T7）")
    g = read_json(p)
    asof = g.get("as_of", "")
    if asof > w.sunday.isoformat():
        raise NoData(f"现存打分日期 {asof} 晚于本周，图谱打分只保留最新一期，不能回看")
    cands = [c for c in g.get("companies", []) if c.get("cross_candidate") is True]
    out = [f"驱动图谱打分日期 {asof}" + ("（不是本周数据，T7 尚未挂每日运行）" if asof < w.monday.isoformat() else "")
           + "。条件：≥2 个独立驱动家族计票，且距 52 周高点回撤达到图谱规则阈值。", ""]
    if not cands:
        out.append("无符合条件的公司。")
    else:
        out += ["|代码|公司|票数|计票家族|正式加权分|距52周高点回撤|计划状态|", "|---|---|---:|---|---:|---:|---|"]
        for c in cands:
            fam = "、".join(f["family"] for f in c.get("families", []) if f.get("vote"))
            out.append(f"|{c['code']}|{c['name']}|{c['votes']}|{fam}|{num(c.get('weighted_score'), 2)}|"
                       f"{pct((c.get('pricing') or {}).get('drawdown_52w'))}|{(c.get('pool') or {}).get('plan_status') or '—'}|")
    out += ["", f"来源：`{rel(p, root)}`"]
    return out, {"打分日期": asof, "候选": [c["name"] for c in cands]}


# ---------------- 判断档案（按本周日 24:00 取状态） ----------------

def load_judgments(root: Path, cutoff: dt.date) -> list[dict]:
    recs = []
    for path in sorted((root / "judgments").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("date", "9999") > cutoff.isoformat():
                continue                      # 本周之后才写入的判断不算
            res = r.get("result")
            r = dict(r)
            if isinstance(res, dict) and res.get("date", "9999") > cutoff.isoformat():
                r["status"], r["result"] = "待验证", None   # 复核发生在本周之后：当时还是待验证
            r["_file"] = rel(path, root)
            recs.append(r)
    return recs


def scope_key(r):
    s = r.get("scope") or {}
    return s.get("type"), s.get("id") or s.get("name")


def sname(r):
    return (r.get("scope") or {}).get("name", "?")


def sec_conflicts(js: list[dict], market: dict | None, two: dict | None, mainline: dict | None) -> tuple[list[str], dict]:
    items = []
    pending = [r for r in js if r.get("status") == "待验证"]
    opp = {"看多": "看空", "攻": "守"}
    groups: dict = {}
    for r in pending:
        groups.setdefault(scope_key(r), []).append(r)
    for key, rs in groups.items():
        dirs = {r["direction"] for r in rs}
        if any(b in dirs for a, b in opp.items() if a in dirs):
            items.append(f"同一对象方向相反：{sname(rs[0])}（" + "；".join(f"{r['id']} {r['module']} {r['direction']}" for r in rs) + "）")
    gate = (market or {}).get("截止日结论")
    if gate == "守":
        for r in pending:
            if r.get("module") in ("产业推演", "驱动图谱") and r.get("direction") == "看多":
                items.append(f"大盘为守，但{r['module']}有看多判断：{r['id']} {sname(r)}（{r['claim'][:40]}…）")
    if two:
        c = two["阶段分布"]
        if two["大盘"] == "守" and c["确认"] == 0:
            items.append("两层对照：大盘为守，且没有任何方向处在确认阶段")
        if c["拥挤"] >= 3:
            items.append(f"两层对照：{c['拥挤']} 个方向同时处在拥挤阶段")
        if two["大盘"] == "守" and c["确认"] > 0:
            items.append("两层对照：大盘为守，但有方向在确认阶段（" + "、".join((mainline or {}).get("确认", [])) + "）")
    if gate is None:
        items.append("本周截止日没有市场状态存档，涉及大盘攻守的冲突无法检查")
    out = [f"- {x}" for x in items] or ["- 无"]
    out += ["", "以上只列出，不裁定；由 Claude 或 Rsan 判断。"]
    return out, {"条数": len([x for x in items if not x.startswith("本周截止日")])}


def sec_due(js: list[dict], w: Week) -> tuple[list[str], dict]:
    mon, sun = w.monday.isoformat(), w.sunday.isoformat()
    due = sorted((r for r in js if mon <= r.get("review_date", "") <= sun), key=lambda r: (r["review_date"], r["id"]))
    overdue = sorted((r for r in js if r.get("review_date", "9999") < mon and r.get("status") == "待验证"),
                     key=lambda r: (r["review_date"], r["id"]))
    out = []

    def line(r):
        res = r.get("result") or {}
        if r.get("status") != "待验证":
            tail = f"**{r['status']}**（{res.get('date')}，{'程序' if r.get('auto') else '人工'}判定）：{res.get('note', '')}"
        elif r.get("check"):
            tail = "程序待判（行情未齐或未到终点，见 `logs/自动复核_最新.md`）"
        else:
            tail = "需人工复核：【待判读】"
        return f"- {r['review_date']} {r['id']} [{r['module']}] {sname(r)}：{r['claim']}  \n  → {tail}"

    out += [f"本周到期 {len(due)} 条：", ""] + ([line(r) for r in due] or ["- 无"])
    if overdue:
        out += ["", f"此前到期、截至本周日仍未复核 {len(overdue)} 条：", ""] + [line(r) for r in overdue]
    return out, {"本周到期": len(due), "此前到期未复核": len(overdue),
                 "需人工": sum(1 for r in due + overdue if r.get("status") == "待验证" and not r.get("check"))}


def sec_hit(js: list[dict]) -> tuple[list[str], dict]:
    tally: dict = {}
    for r in js:
        s = r.get("status")
        if s in ("证实", "证伪", "部分"):
            key = (r["module"], "程序" if r.get("auto") else "手写", r["confidence"])
            tally.setdefault(key, {"证实": 0, "证伪": 0, "部分": 0})[s] += 1
    total = len(js)
    waiting = sum(1 for r in js if r.get("status") == "待验证")
    out = [f"截至本周日：判断共 {total} 条，待验证 {waiting} 条。口径同 `check_judgments.py --stats`（证实＝1，部分＝0.5，无法判定不计分母）。", ""]
    if not tally:
        out.append("还没有复核完成的判断。")
    else:
        out += ["|模块|来源|置信度|证实|部分|证伪|命中率|", "|---|---|---|---:|---:|---:|---:|"]
        for (m, a, c), t in sorted(tally.items()):
            n = t["证实"] + t["部分"] + t["证伪"]
            out.append(f"|{m}|{a}|{c}|{t['证实']}|{t['部分']}|{t['证伪']}|{(t['证实'] + 0.5 * t['部分']) / n:.0%}|")
    return out, {"总数": total, "待验证": waiting, "分组": len(tally)}


def sec_verdicts(js: list[dict], w: Week) -> tuple[list[str], dict]:
    mon, sun = w.monday.isoformat(), w.sunday.isoformat()
    done = [r for r in js if r.get("status") not in ("待验证", "已被取代") and isinstance(r.get("result"), dict)
            and mon <= r["result"].get("date", "") <= sun]
    out = [f"- {r['result']['date']} **{r['status']}** {r['id']} [{r['module']}] {sname(r)}：{r['claim']}（{r['result'].get('note', '')}）"
           for r in sorted(done, key=lambda r: (r["result"]["date"], r["id"]))] or ["- 本周没有判断被复核。"]
    return out, {"证实": sum(r["status"] == "证实" for r in done), "证伪": sum(r["status"] == "证伪" for r in done),
                 "部分": sum(r["status"] == "部分" for r in done), "无法判定": sum(r["status"] == "无法判定" for r in done)}


# ---------------- 组装 ----------------

def run_section(fn, *args):
    try:
        lines, info = fn(*args)
        return lines, info, None
    except NoData as e:
        return [f"暂无数据，原因：{e}"], None, str(e)


def build(root: Path, w: Week, now: dt.datetime) -> tuple[str, dict]:
    if not w.days:
        raise ValueError(f"{w.label} 没有交易日")
    js = load_judgments(root, w.sunday)
    secs, info, missing = [], {}, {}

    def add(title, fn, *args):
        lines, i, why = run_section(fn, *args)
        secs.append((title, lines))
        info[title] = i
        if why:
            missing[title] = why
        return i

    add("1. 散户情绪", sec_sentiment, root, w)
    market = add("2. 大盘攻守", sec_market, root, w)
    two = add("3. 两层对照", sec_two_layer, root, w, market)
    add("4. 资金在追什么、在撤什么", sec_flow, root, w)
    add("5. 海外市场一周涨跌", sec_overseas, root, w)
    mainline = add("6. 主线雷达", sec_mainline, root, w)
    add("7. 交叉候选", sec_cross, root, w)
    add("8. 冲突清单", sec_conflicts, js, market, two, mainline)
    add("9. 到期复核", sec_due, js, w)
    add("10. 命中率", sec_hit, js)
    add("11. 本周证实/证伪", sec_verdicts, js, w)

    lines = [f"# {TITLE} · {w.last.isoformat()}", "",
             f"本周：{w.label}（交易日 {len(w.days)} 天，数据截止 {w.last.isoformat()}）；"
             f"生成：{now.isoformat(timespec='seconds')}（北京时间）；`scripts/panorama.py`。", "",
             "程序只汇总各模块已算好的指标和规则结论，不重算、不下结论；【待判读】处由 Claude 补写。", ""]
    if missing:
        lines += ["缺数据的栏目：" + "；".join(f"{k}（{v}）" for k, v in missing.items()), ""]
    for title, body in secs:
        lines += [f"## {title}", ""] + body + [""]
    lines += ["---", "", "以上为程序汇总的指标与规则结论，不构成投资建议。", ""]
    return "\n".join(lines), {"栏目": info, "缺数据栏目": missing}


def readiness(root: Path, last: dt.date) -> list[str]:
    """--auto 生成前要求各上游产出已覆盖本周截止日；返回还没覆盖的项。"""
    d = last.isoformat()
    lacks = []
    checks = [
        ("市场状态", root / "data/市场状态" / last.strftime("%Y-%m") / f"{d}.json", None),
        ("散户情绪温度", root / "data/社区情绪/温度计.csv", "csv"),
        ("资金轮动", root / "data/资金轮动/最新.json", "data_date"),
        ("主线阶段", root / "data/主线/阶段_最新.json", "数据日期"),
        ("自动复核", root / "logs/自动复核_最新.json", "data_date"),
    ]
    for name, p, key in checks:
        try:
            if key is None:
                ok = p.exists()
            elif key == "csv":
                ok = d in set(pd.read_csv(p, encoding="utf-8-sig", usecols=["date"], dtype=str)["date"])
            else:
                ok = str(read_json(p).get(key, "")) >= d
        except (OSError, ValueError, KeyError):
            ok = False
        if not ok:
            lacks.append(name)
    return lacks


def save_log(root: Path, report: dict):
    old = root / "logs/全景_最新.json"
    if report["status"] == "失败" and old.exists():
        try:
            report.setdefault("last_success_at", read_json(old).get("last_success_at"))
        except (OSError, ValueError):
            pass
    atomic_text(old, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    md = [f"# {report['title']}", "", f"北京时间：{report['generated_at']}", f"数据日期：{report['data_date']}", "",
          f"状态：{report['status']}", ""]
    if report.get("file"):
        md.append(f"全景：`{report['file']}`")
    for key, title in (("missing", "缺数据栏目"), ("errors", "失败"), ("notes", "说明")):
        vals = report.get(key)
        if vals:
            items = [f"{k}：{v}" for k, v in vals.items()] if isinstance(vals, dict) else list(vals)
            md += ["", f"## {title}", ""] + [f"- {x}" for x in items]
    atomic_text(root / "logs/全景_最新.md", "\n".join(md) + "\n")


def generate(root: Path, w: Week, now: dt.datetime, out_dir: Path, *, write_log: bool, notes=None) -> Path:
    text, meta = build(root, w, now)
    target = out_dir / f"{w.last.isoformat()}.md"
    atomic_text(target, text)
    if write_log:
        save_log(root, {"title": TITLE, "generated_at": now.isoformat(), "data_date": w.last.isoformat(),
                        "status": "正常", "errors": [],  # 缺数据栏目是已知缺口（如 T3 未完成），单列在 missing，不算失败
                        "missing": meta["缺数据栏目"], "notes": notes or [], "file": rel(target, root),
                        "last_success_at": now.isoformat(), "week": w.label})
    print(f"已生成 {rel(target, root) if target.is_relative_to(root) else target}；缺数据栏目：{list(meta['缺数据栏目']) or '无'}")
    return target


def auto(root: Path, now: dt.datetime) -> int:
    w = Week(root, now.date())
    if not w.days:
        if now.weekday() >= 4:
            save_log(root, {"title": TITLE, "generated_at": now.isoformat(), "data_date": now.date().isoformat(),
                            "status": "正常", "errors": [], "notes": [f"{w.label} 全周休市，本周不出全景"],
                            "file": None, "last_success_at": now.isoformat(), "week": w.label})
            print("本周全周休市，只更新运行记录")
        return 0
    if now.date() < w.last:
        print(f"本周最后交易日是 {w.last}，还没到，不生成")
        return 0
    target = root / "panorama" / f"{w.last.isoformat()}.md"
    if target.exists():
        print(f"{rel(target, root)} 已存在，每周只自动写一次（Claude 可能已补写【待判读】），跳过")
        return 0
    lacks = readiness(root, w.last)
    deadline = dt.datetime.combine(w.monday + dt.timedelta(days=DEADLINE_WEEKDAY), DEADLINE_TIME, BJ)
    if lacks and now < deadline:
        print(f"上游还没覆盖 {w.last}：{'、'.join(lacks)}；等到 {deadline.isoformat()} 前的下一次触发")
        return 0
    notes = [f"到截止 {deadline.isoformat()} 仍未覆盖本周截止日的上游：{'、'.join(lacks)}，相关栏目按现有数据生成"] if lacks else []
    generate(root, w, now, root / "panorama", write_log=True, notes=notes)
    return 0


# ---------------- Issue 提醒 ----------------

def issue(root: Path, now: dt.datetime) -> int:
    log = read_json(root / "logs/全景_最新.json")
    if not log.get("file") or log.get("status") == "失败":
        print("最新运行没有生成全景，不开 Issue")
        return 0
    date = log["data_date"]
    record = root / "logs/全景提醒记录.csv"
    if record.exists() and any(r["数据截止日"] == date for r in csv.DictReader(record.open(encoding="utf-8"))):
        print(f"{date} 的全景已提醒过")
        return 0
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("不在 Actions，只打印不联网")
        return 0
    from health_alert import GitHub
    repo = os.environ["GITHUB_REPOSITORY"]
    api = GitHub(repo, os.environ.get("GITHUB_TOKEN", ""))
    try:
        api.request("GET", "labels/" + __import__("urllib.parse").parse.quote(LABEL, safe=""))
    except Exception:
        api.request("POST", "labels", {"name": LABEL, "color": "1D76DB", "description": "每周市场全景"})
    text = (root / log["file"]).read_text(encoding="utf-8")
    first = next((l for l in text.split("## 1. 散户情绪", 1)[-1].splitlines() if l.startswith("情绪温度")), "")
    url = f"https://github.com/{repo}/blob/main/{log['file']}"
    body = "\n".join([f"本周（{log.get('week', '')}）市场全景已生成：{url}", "", first, "",
                      "缺数据栏目：" + ("、".join(log.get("missing") or {}) or "无"),
                      "【待判读】栏目由 Claude 补写。不构成投资建议。"])
    res = api.request("POST", "issues", {"title": f"{TITLE} {date}", "body": body, "labels": [LABEL]})
    new = not record.exists()
    with record.open("a", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=ISSUE_LOG_FIELDS, lineterminator="\n")
        if new:
            wr.writeheader()
        wr.writerow({"数据截止日": date, "issue": res.get("number", ""), "记录时间": now.isoformat()})
    print(f"已开 Issue #{res.get('number')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--week", help="生成该日期所在周的全景（YYYY-MM-DD）")
    g.add_argument("--auto", action="store_true")
    g.add_argument("--issue", action="store_true")
    ap.add_argument("--out-dir", help="--week 的输出目录，默认 panorama/；隔离测试用 logs/test/T5")
    ap.add_argument("--force", action="store_true", help="--week 写 panorama/ 时允许覆盖已存在的一期")
    ap.add_argument("--now", help="以此时刻为现在（测试用，ISO 格式，北京时间）")
    ap.add_argument("--root", type=Path, default=ROOT)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    now = dt.datetime.fromisoformat(a.now).replace(tzinfo=BJ) if a.now else dt.datetime.now(BJ)
    try:
        if a.auto:
            return auto(root, now)
        if a.issue:
            return issue(root, now)
        w = Week(root, dt.date.fromisoformat(a.week))
        out = (root / a.out_dir) if a.out_dir else root / "panorama"
        if not a.out_dir and w.last and (out / f"{w.last.isoformat()}.md").exists() and not a.force:
            raise FileExistsError(f"panorama/{w.last.isoformat()}.md 已存在（可能已有 Claude 补写的判读），要覆盖请加 --force")
        generate(root, w, now, out, write_log=not a.out_dir)
        return 0
    except Exception as e:  # 写明失败，不写坏全景
        if a.auto:
            save_log(root, {"title": TITLE, "generated_at": now.isoformat(), "data_date": now.date().isoformat(),
                            "status": "失败", "errors": [f"{type(e).__name__}: {e}"], "file": None})
        print(f"失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
