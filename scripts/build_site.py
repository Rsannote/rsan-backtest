#!/usr/bin/env python3
"""看板数据生成（T16 v2）。只读取已有产出、不重算，写 site/data/dashboard.json 给 site/index.html。

看板按"关注的问题"组织，每个问题一个答案、一段怎么读、一段 Claude 的判读（取最新一期全景）：
  大盘能不能进攻（攻守、冷热、情绪温度）→ 钱往哪走 → 有没有主线 → 像历史哪次 → 海外怎么样 → 这周盯什么。
程序只搬运指标和已写好的判读，不下结论；某一块出错只影响那一块。

用法：python3 scripts/build_site.py [--out site/data]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
BJ = dt.timezone(dt.timedelta(hours=8))
ZONES = [(20, "冰点"), (40, "偏冷"), (60, "中性"), (80, "偏热"), (101, "过热")]   # 同 system/情绪规则.md
REPO = "Rsannote/rsan-backtest"


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    return value


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def zone(t):
    return next(name for hi, name in ZONES if t < hi)


def health(root: Path) -> dict:
    report = read_json(root / "logs/健康检查_最新.json") or {"items": []}
    items = {i["id"]: i for i in report["items"]}
    return {"检查时间": report.get("checked_at"), "项": items}


def stale(h: dict, *ids: str) -> bool:
    return any(h["项"].get(i, {}).get("status") in ("延迟", "失败") for i in ids)


# ------------------------------------------------------------------ 最新一期全景里的 Claude 判读

def panorama(root: Path) -> dict:
    files = sorted((root / "panorama").glob("20*.md"))
    if not files:
        return {}
    path = files[-1]
    text = path.read_text(encoding="utf-8")

    def block(start, stop=r"\n来源：|\n## "):
        m = re.search(start + r"[^\n]*\n?(.*?)(?=" + stop + r"|\Z)", text, re.S)
        if not m:
            return None
        body = m.group(1).strip()
        return None if not body or "【待判读】" in body[:20] else body

    def drop_watch(body):
        if not body:
            return body
        return "\n".join(l for l in body.splitlines() if "要盯" not in l).strip() or None

    summary = re.search(r"\*\*Claude 小结（[^）]*）\*\*：(.+)", text)
    watch = []
    for part in (block(r"和历史哪次最像、要盯什么（Claude 判读"), block(r"像在哪、不像在哪（Claude 判读")):
        for line in (part or "").splitlines():
            if "要盯" in line:
                watch.append(re.sub(r"^\s*-\s*", "", line).strip())
    return {"日期": path.stem, "链接": f"https://github.com/{REPO}/blob/main/panorama/{path.name}",
            "小结": summary.group(1).strip() if summary else None,
            "主线判读": drop_watch(block(r"和历史哪次最像、要盯什么（Claude 判读")),
            "历史判读": drop_watch(block(r"像在哪、不像在哪（Claude 判读")),
            "冲突判读": block(r"\nClaude 判读（", r"\n## "), "要盯": watch}


# ------------------------------------------------------------------ 各问题

def market(root: Path, h: dict) -> dict:
    state = read_json(root / "data/市场状态/最新.json") or {}
    m = state.get("市场", {})
    temp = pd.read_csv(root / "data/社区情绪/温度计.csv", encoding="utf-8-sig", dtype={"date": str}).tail(120)
    t = float(temp["温度"].iloc[-1])
    import panorama as pano
    last = dt.date.fromisoformat(str(temp["date"].iloc[-1]))
    stage, stage_date, _ = pano.latest_stage(root, pano.Week(root, last))
    hs = m.get("沪深300", {})
    two = m.get("两融", {})
    return {"数据日期": state.get("数据日期"), "过期": stale(h, "market_state", "sentiment_index"),
            "结论": m.get("结论"), "冷热": m.get("冷热"), "总分": m.get("总分"), "信号": m.get("信号", []),
            "依据": m.get("依据", []), "冷信号": m.get("冷信号", []), "热信号": m.get("热信号", []),
            "温度": round(t, 1), "温度区间": zone(t), "温度日期": str(temp["date"].iloc[-1]),
            "温度5日前": float(temp["温度"].iloc[-6]) if len(temp) > 5 else None,
            "阶段": stage, "阶段日期": stage_date,
            "温度走势": {"date": temp["date"].tolist(), "温度": temp["温度"].round(1).tolist()},
            "沪深300": {"涨跌": hs.get("chg"), "5日": hs.get("chg5"), "偏离20日线": hs.get("dev20"),
                       "在60日线上": hs.get("above60"), "20日线向上": hs.get("ma20向上")},
            "成交额亿": m.get("成交额"), "成交额分位": m.get("成交额分位"), "涨停": m.get("涨停"), "跌停": m.get("跌停"),
            "融资余额亿": two.get("余额亿"), "融资5日变化": two.get("5日变化"), "融资日期": two.get("日期")}


def flow(root: Path, h: dict) -> dict:
    cols = ["date", "code", "name", "占比_31", "占比5日变化_31", "相对沪深300_5", "占比一年分位_31"]
    df = pd.read_csv(root / "data/资金轮动/日表.csv", usecols=cols, dtype={"code": str, "date": str})
    df = df.drop_duplicates(["date", "code"], keep="last")
    day = df["date"].max()
    t = df[df["date"] == day].dropna(subset=["占比5日变化_31"]).sort_values("占比5日变化_31", ascending=False)

    def rows(sub):
        return [{"行业": r["name"], "占比": r["占比_31"], "变化": r["占比5日变化_31"], "相对沪深300": r["相对沪深300_5"],
                 "一年分位": r["占比一年分位_31"]} for _, r in sub.iterrows()]

    state = read_json(root / "data/市场状态/最新.json") or {}
    style = (state.get("板块") or {}).get("风格结论")
    return {"数据日期": day, "过期": stale(h, "flow"), "风格": style, "在追": rows(t.head(5)), "在撤": rows(t.tail(5).iloc[::-1])}


def mainline(root: Path, h: dict) -> dict:
    latest = read_json(root / "data/主线/阶段_最新.json") or {}
    ev = pd.read_csv(root / "data/主线/阶段事件_31.csv", dtype={"code": str, "date": str})
    stages = {}
    for r in latest.get("行业", []):
        if r["阶段"] in ("无", None):
            continue
        e = ev[ev["code"] == r["code"]]
        since = e[e["to"] == r["阶段"]]["date"].max() if not e.empty else None
        start = e[e["里程碑"].fillna("").str.contains("启动")]
        kind = start.iloc[-1]["上位类型"] if not start.empty and isinstance(start.iloc[-1]["上位类型"], str) else None
        stages.setdefault(r["阶段"], []).append({"行业": r["行业"], "自": since, "上位类型": kind or "未明",
                                                "占比一年分位": r.get("占比一年分位"), "偏离60日线": r.get("偏离60日线")})
    crowd = sorted(({"行业": r["行业"], "占比一年分位": r["占比一年分位"], "偏离60日线": r.get("偏离60日线"), "阶段": r["阶段"]}
                    for r in latest.get("行业", []) if (r.get("占比一年分位") or 0) >= 90),
                   key=lambda x: -x["占比一年分位"])
    return {"数据日期": latest.get("数据日期"), "过期": stale(h, "mainline_signals", "mainline_stage"),
            "参数版本": latest.get("参数版本"), "阶段": stages, "行业数": len(latest.get("行业", [])),
            "成交拥挤": crowd, "近5日变化": len(latest.get("近5日阶段变化", []))}


def history(root: Path, h: dict) -> dict:
    cmp = read_json(root / "data/事件库/当前对照.json") or {}
    hs = pd.read_csv(root / "data/行情库/指数/sh000300.csv", dtype={"date": str})
    hs["p"] = hs["close"] * hs["factor"]
    idx = {d: i for i, d in enumerate(hs["date"])}
    rows = []
    for r in (cmp.get("主表排名") or [])[:3]:
        i = idx.get(r["开始"])
        path = []
        if i is not None:
            seg = hs["p"].iloc[i:i + 61]
            path = [round(float(v / seg.iloc[0] - 1) * 100, 2) for v in seg]
        rows.append({"类型": r["类型"], "开始": r["开始"], "结束": r["结束"], "距离": r["距离"],
                     "之后60日": path, "特征": r.get("历史特征", {})})
    keys = ["启动前60日涨幅", "启动时成交额分位", "启动时融资余额变化", "启动时情绪温度", "启动时中国10年国债", "启动时美国10年国债"]
    cur = cmp.get("当前特征", {})
    return {"数据日期": cmp.get("data_date"), "过期": stale(h, "events"), "特征名": keys,
            "当前": {k: cur.get(k) for k in keys}, "最像": rows}


def overseas(root: Path, h: dict) -> dict:
    lst = pd.read_csv(root / "data/标的清单.csv", dtype=str)
    items = lst[lst["group"] == "海外指数"]
    rows, missing = [], []
    for _, it in items.iterrows():
        p = root / "data/行情库/指数" / f"{it['code']}.csv"
        if not p.exists():
            missing.append(it["name"])
            continue
        t = pd.read_csv(p, dtype={"date": str})
        if len(t) < 21:
            continue
        rows.append({"指数": it["name"], "日期": t["date"].iloc[-1], "收盘": t["close"].iloc[-1],
                     "5日": t["close"].iloc[-1] / t["close"].iloc[-6] - 1, "20日": t["close"].iloc[-1] / t["close"].iloc[-21] - 1})
    macro = []
    now = dt.datetime.now(BJ).isoformat()
    derived = pd.read_csv(root / "data/宏观/派生.csv", dtype=str) if (root / "data/宏观/派生.csv").exists() else pd.DataFrame()
    for name in ("中国10年国债", "美国10年国债", "美元兑人民币"):
        p = root / f"data/宏观/{name}.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p, dtype=str)
        df = df[df["可见时刻"] <= now].drop_duplicates("date", keep="last")
        if df.empty:
            continue
        chg = None
        if not derived.empty:
            d = derived[(derived["指标"] == name) & (derived["可见时刻"] <= now)]
            chg = float(d.iloc[-1]["value"]) if not d.empty else None
        macro.append({"指标": name, "日期": df.iloc[-1]["date"], "值": float(df.iloc[-1]["value"]), "20日变化bp": chg})
    return {"数据日期": max([r["日期"] for r in rows], default=None), "过期": stale(h, "hangqing", "macro"),
            "指数": rows, "缺": missing, "宏观": macro}


def watchlist(root: Path, now: dt.datetime) -> dict:
    rows = []
    for path in sorted((root / "judgments").glob("*.jsonl")):
        rows += [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    today = now.date().isoformat()
    soon = (now.date() + dt.timedelta(days=14)).isoformat()
    due = sorted(({"复核日": r["review_date"], "对象": r["scope"].get("name"), "判断": r["claim"], "模块": r["module"],
                   "程序": bool(r.get("auto"))}
                  for r in rows if r.get("status") == "待验证" and r.get("review_date", "") <= soon and not r.get("auto")),
                 key=lambda x: x["复核日"])
    done = [r for r in rows if r.get("status") in ("证实", "证伪", "部分")]
    hit = (sum(r["status"] == "证实" for r in done) + 0.5 * sum(r["status"] == "部分" for r in done)) / len(done) if done else None
    auto_due = sum(1 for r in rows if r.get("status") == "待验证" and r.get("auto") and r.get("review_date", "") <= soon)
    return {"今天": today, "近两周到期": due, "程序判断近两周到期": auto_due,
            "判断": {"总数": len(rows), "待验证": sum(r.get("status") == "待验证" for r in rows), "已复核": len(done), "命中率": hit}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    out = args.out or root / "site/data"
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(BJ)
    h = health(root)
    data = {"生成时间": now.isoformat(timespec="minutes"), "错误": []}
    for name, func in {"大盘": market, "资金": flow, "主线": mainline, "历史": history, "海外": overseas}.items():
        try:
            data[name] = func(root, h)
        except Exception as exc:  # 一块失败不影响其他
            data[name] = {"错误": f"{type(exc).__name__}: {exc}", "过期": True}
            data["错误"].append(f"{name}: {exc}")
    for name, func in {"判读": lambda: panorama(root), "盯": lambda: watchlist(root, now)}.items():
        try:
            data[name] = func()
        except Exception as exc:
            data[name] = {}
            data["错误"].append(f"{name}: {exc}")
    data["系统"] = {"检查时间": h["检查时间"],
                  "问题": [{"名称": i["name"], "状态": i["status"], "原因": (i.get("reasons") or [""])[0][:120]}
                         for i in h["项"].values() if i["status"] != "正常"]}
    for old in out.glob("*.json"):           # v1 的分栏文件不再使用
        if old.name != "dashboard.json":
            old.unlink()
    text = json.dumps(clean(data), ensure_ascii=False, allow_nan=False)
    (out / "dashboard.json").write_text(text + "\n", encoding="utf-8")
    print(f"看板数据：{out / 'dashboard.json'}，{len(text.encode()) / 1024:.0f} KB；错误 {data['错误'] or '无'}", flush=True)
    return 1 if len(data["错误"]) >= 5 else 0


if __name__ == "__main__":
    raise SystemExit(main())
