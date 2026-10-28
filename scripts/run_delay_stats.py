#!/usr/bin/env python3
"""从 Git 产出提交与 Actions 元数据校准健康检查；不抓行情，不改 data。

历史成功提交是产出可见的时刻，不把 run_started_at 当计划时刻。
多时段的历史 API 不含触发 cron，顺序配对仅用于展示估计；告警门槛
使用无需 cron 归属推断的成功产出间隔。联网收集只允许在 Actions。
"""
import argparse
import copy
from datetime import datetime, timedelta
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

import health_check as hc

ROOT = Path(__file__).resolve().parents[1]
SPECS = {
    "hangqing": ("行情库更新 ", "logs/行情库_最新.md"),
    "market_state": ("市场状态 ", "data/市场状态/最新.json"),
    "sentiment_index": ("散户情绪指标 ", "data/社区情绪/触发.json"),
    "community": ("社区情绪 ", "data/社区情绪"),
    "pool": ("股票池镜像 ", "data/股票池/状态.json"),
    "news": ("新闻库 ", "data/新闻/状态.json"),
    "tophub": ("信息源 ", "data/信息源/tophub/运行记录.txt"),
    "v3": ("业绩加速选股 ", "data/v3/latest.json"),
}
DAILY = {"hangqing", "market_state", "sentiment_index", "community", "pool"}


def git(root, *args, binary=False):
    return subprocess.check_output(["git", "-c", "core.quotePath=false", "-C", str(root), *args], text=not binary)


def quantiles(values):
    """最近秩分位数，小样本的 P95 不插值低估尾部。"""
    a = sorted(values)
    if not a:
        return {"n": 0, "p50": None, "p90": None, "p95": None, "max": None}
    return {"n": len(a), **{key: a[max(0, math.ceil(len(a) * q) - 1)]
                              for key, q in (("p50", .5), ("p90", .9), ("p95", .95), ("max", 1))}}


def allowance(stats):
    if not stats["n"]:
        return None
    # 余量来自尾部离散程度；最少一个输出配置分钟用于时间取整。
    margin = max(stats["p95"] - stats["p50"], stats["max"] - stats["p95"], 1)
    return {"p95_minutes": stats["p95"], "margin_minutes": margin,
            "minutes": math.ceil(stats["p95"] + margin)}


def history(root, ref):
    rows = []
    # first-parent 避免把分支验收模拟数据/合并内的临时提交当成生产历史。
    visible = None
    for line in git(root, "log", "--first-parent", "--reverse", "--format=%H\t%cI\t%an\t%s", ref).splitlines():
        sha, at, author, title = line.split("\t", 3)
        at = hc.timestamp(at)
        visible = max(visible, at) if visible else at
        rows.append({"sha": sha, "at": visible, "commit_time": at, "author": author, "title": title})
    return rows


def show(root, sha, path):
    return git(root, "show", f"{sha}:{path}")


def introduced(root, ref, path):
    rows = git(root, "log", "--first-parent", "--diff-filter=A", "--format=%cI", ref, "--", path).splitlines()
    return min(map(hc.timestamp, rows)) if rows else None


def collect_actions(root, now, out):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Actions 元数据联网收集只能在 GitHub Actions 执行")
    from health_alert import GitHub
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    since = (now - timedelta(days=28)).astimezone(hc.dt.timezone.utc).isoformat()
    import urllib.parse
    runs = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"branch": "main", "created": ">=" + since, "per_page": 100, "page": page})
        batch = api.request("GET", "actions/runs?" + query)["workflow_runs"]
        runs.extend({k: r.get(k) for k in ("id", "path", "event", "status", "conclusion", "created_at", "updated_at", "run_started_at", "html_url")} for r in batch)
        if len(batch) < 100:
            break
        page += 1
    data = {"captured_at": now.isoformat(), "repository": api.repository, "runs": runs}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def run_for(commit, item, runs):
    if not item.get("workflow"):
        return {"event": "external", "id": None}
    matches = [r for r in runs if r["path"] == item["workflow"] and r["event"] in ("schedule", "workflow_run")
               and r["status"] == "completed" and r["conclusion"] == "success"
               and hc.timestamp(r["created_at"]) <= commit["at"] <= hc.timestamp(r["updated_at"])]
    return min(matches, key=lambda r: hc.timestamp(r["updated_at"])) if matches else None


def produced(root, commit, item):
    key = item["id"]
    raw = show(root, commit["sha"], SPECS[key][1]) if key != "community" else ""
    at = commit["at"]
    success = True
    if key == "hangqing":
        target = re.search(r"最新交易日[：:]\s*(\d{4}-\d{2}-\d{2})", raw)[1]
        run_at = hc.first_stamp(raw)
    elif key == "community":
        paths = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit["sha"]).splitlines()
        path = next(p for p in paths if re.fullmatch(r"data/社区情绪/\d{4}-\d{2}-\d{2}/运行记录.txt", p))
        target = path.split("/")[2]
        run_at = hc.first_stamp(show(root, commit["sha"], path))
    elif key == "tophub":
        stamps = list(re.finditer(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$", raw, re.M))
        last = stamps[-1]
        run_at = hc.timestamp(last[1])
        target = run_at.date().isoformat()
        success = bool(re.search(r"\t[1-9]\d*条(?:\t|$)", raw[last.end():], re.M))
    else:
        obj = json.loads(raw)
        if key == "market_state":
            target, run_at = obj["数据日期"], hc.timestamp(obj["生成时间"])
        elif key == "sentiment_index":
            target, run_at = obj["date"], at
        elif key == "pool":
            target, run_at = obj["数据日期"], hc.timestamp(obj["最近检查"])
        elif key == "news":
            run_at = hc.timestamp(obj["最近运行"])
            target = run_at.date().isoformat()
            success = any(v.get("ok") is True for v in obj["来源"].values())
        else:
            target, run_at = obj["asof"], hc.timestamp(obj["generated"])
            success = not obj.get("incomplete", False)
    return str(target)[:10], run_at, success


def slot_sequence(item, start, end):
    date = start.date()
    while date <= end.date():
        for clock in sorted(item["times"]):
            value = hc.scheduled(date, clock)
            if start <= value <= end:
                yield value
        date += timedelta(days=1)


def analyze(root, ref, manifest, runs_data, now):
    ref = git(root, "rev-parse", ref).strip()
    commits = history(root, ref)
    begin = now - timedelta(days=28)
    result = {"schema_version": 1, "checked_at": now.isoformat(), "requested_since": begin.isoformat(),
              "source_ref": ref, "first_repository_commit": commits[0]["at"].isoformat(),
              "formula": "ceil(P95 + max(P95-P50, 最大值-P95, 1分钟))；最近秩分位数",
              "items": {}, "excluded": []}
    run_rows = runs_data["runs"]
    for item in manifest["items"]:
        key = item["id"]
        if key not in SPECS:
            continue
        inception = introduced(root, ref, item["workflow"]) if item.get("workflow") else introduced(root, ref, SPECS[key][1])
        rows = []
        for commit in commits:
            if not begin <= commit["at"] <= now or commit["author"] != "github-actions" or not commit["title"].startswith(SPECS[key][0]):
                continue
            run = run_for(commit, item, run_rows)
            if not run:
                result["excluded"].append({"item": key, "sha": commit["sha"], "reason": "无成功 schedule/workflow_run 匹配；手动运行不校准调度延迟"})
                continue
            try:
                target, internal_at, success = produced(root, commit, item)
            except (ValueError, KeyError, IndexError, StopIteration, subprocess.CalledProcessError) as exc:
                result["excluded"].append({"item": key, "sha": commit["sha"], "reason": f"产出解析失败：{exc}"})
                continue
            rows.append({"sha": commit["sha"], "available_at": commit["at"].isoformat(), "output_at": internal_at.isoformat(),
                         "data_date": target, "run_id": run["id"], "success": success})
        multi = key in ("news", "tophub")
        slots = list(slot_sequence(item, inception, now)) if multi and inception else []
        delays, rotations = [], []
        for i, row in enumerate(rows):
            at = hc.timestamp(row["available_at"])
            weekly = key in ("hangqing", "market_state", "sentiment_index") and at.weekday() == 5
            if multi:
                if i >= len(slots):
                    row["mapping_warning"] = "运行数超过可配对时段"
                    continue
                nominal = slots[i]
                row["mapping"] = "从工作流上线后第一个时段起，成功定时运行按顺序配对的估计；API未提供原始cron，不用于新鲜度门槛"
            else:
                nominal = hc.scheduled(at.date() if weekly else hc.day(row["data_date"]), "10:17" if weekly else item["times"][0])
                row["mapping"] = "产出数据日期+清单计划；周六行情链使用轮换计划" if weekly else "产出数据日期+清单计划"
            delay = (at - nominal).total_seconds() / 60
            row.update(scheduled_at=nominal.isoformat(), delay_minutes=delay, weekly_rotation=weekly)
            if delay < 0 or delay >= 24 * 60:
                row["mapping_warning"] = "归属不确定或超过一个日周期，排除延迟分位数；原始样本保留"
            else:
                (rotations if weekly else delays).append(delay)
        successful = [r for r in rows if r["success"]]
        gaps = [(hc.timestamp(b["available_at"]) - hc.timestamp(a["output_at"])).total_seconds() / 60
                for a, b in zip(successful, successful[1:])]
        delay_stats, gap_stats = quantiles(delays), quantiles(gaps)
        result["items"][key] = {"name": item["name"], "introduced_at": inception.isoformat() if inception else None,
            "delay": delay_stats, "grace": allowance(delay_stats), "rotation_delay": quantiles(rotations),
            "rotation_grace": allowance(quantiles(rotations)), "success_gap": gap_stats, "freshness": allowance(gap_stats),
            "samples": rows, "gap_definition": "下一成功产出提交可见时刻 - 上一成功产出字段时刻（含执行/提交耗时）"}
    return result


def calibrated(manifest, stats):
    out = copy.deepcopy(manifest)
    measured = [row["grace"]["minutes"] for row in stats["items"].values() if row["grace"]]
    fallback = max(measured) if measured else None
    for item in out["items"]:
        key = item["id"]
        if key not in stats["items"]:
            continue
        row = stats["items"][key]
        basis = {"statistics_at": stats["checked_at"], "source_ref": stats["source_ref"], "sample_n": row["delay"]["n"],
                 "p95_minutes": row["delay"]["p95"], "formula": stats["formula"], "report": "logs/运行延迟统计.md"}
        if row["grace"]:
            item["grace_minutes"] = row["grace"]["minutes"]
            basis["margin_minutes"] = row["grace"]["margin_minutes"]
        elif fallback is not None:
            item["grace_minutes"] = fallback
            basis["fallback"] = "无本项定时样本；临时借用全部已测任务中最大的 P95+余量，不声称完成本项校准"
        if key in DAILY:
            item["check_mode"] = "previous_trading_day"
            item["trading_days_only"] = True
        if key in ("news", "tophub"):
            if not row["freshness"]:
                raise ValueError(f"{key}成功间隔样本不足，不写新鲜度门槛")
            item["check_mode"] = "freshness"
            item["max_age_minutes"] = row["freshness"]["minutes"]
            basis["freshness"] = {**row["freshness"], "sample_n": row["success_gap"]["n"], "definition": row["gap_definition"]}
        if item.get("extra_schedule"):
            rotation = row["rotation_grace"]
            item["extra_schedule"]["grace_minutes"] = rotation["minutes"] if rotation else fallback
            basis["rotation"] = rotation or {"fallback_minutes": fallback}
        item["delay_basis"] = basis
    out["说明"] = "时间均为北京时间。收盘相关任务08:20只核对严格上一交易日；22:30只要求计划+实测P95及余量已到的产出。新闻/信息源按最近成功产出的统计间隔检查，不核对单个slot。失败证据不被宽限掩盖。详见每项delay_basis与运行延迟统计。"
    return out


def render(stats):
    lines = ["# 运行延迟统计", "", f"统计时刻：{stats['checked_at']}（北京时间）。请求近28天，自 {stats['requested_since']}。",
             f"生产历史源：`{stats['source_ref']}`；仓库首个可审计提交：{stats['first_repository_commit']}。不足28天不补造。", "",
             "可见时刻取 first-parent 产出提交时间，含排队、执行、提交。只用匹配成功 schedule/workflow_run 的自动提交；外部股票池单列。失败、手动任务不拿来放宽调度门槛。", "",
             "分位数为最近秩，宽限=" + stats["formula"] + "。余量由尾部离散程度计算，1分钟仅用于整数分钟配置取整；这是历史样本内校准，不能保证未来延迟永不超界。", "",
             "| 任务 | 样本 | 中位数/分 | P90/分 | P95/分 | 最大/分 | P95+余量/分 |", "|---|---:|---:|---:|---:|---:|---:|"]
    f = lambda v: "无样本" if v is None else f"{v:.2f}"
    for key, row in stats["items"].items():
        d = row["delay"]
        lines.append(f"| {row['name']} | {d['n']} | {f(d['p50'])} | {f(d['p90'])} | {f(d['p95'])} | {f(d['max'])} | {row['grace']['minutes'] if row['grace'] else '借用已测最大宽限，待样本'} |")
    lines += ["", "新闻/信息源的历史 Actions API 不包含原始触发 cron。上表这两项的计划归属是从工作流上线后首个时段起顺序配对的估计，原始样本逐条标注；不将其用于告警截止。新鲜度门槛直接统计成功产出间隔，不需要猜测slot。", "",
              "| 高频任务 | 成功间隔样本 | 中位数/分 | P90/分 | P95/分 | 最大/分 | 允许距上次成功/分 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for key in ("news", "tophub"):
        row = stats["items"][key]; d = row["success_gap"]
        lines.append(f"| {row['name']} | {d['n']} | {f(d['p50'])} | {f(d['p90'])} | {f(d['p95'])} | {f(d['max'])} | {row['freshness']['minutes'] if row['freshness'] else '缺'} |")
    lines += ["", "成功间隔=下一次成功提交可见时刻减上一次成功字段时刻，包含执行/提交时长。无本项样本的业绩加速选股临时借用已测最大宽限，等待换股月数据；不报告不存在的P95。股票池只有一次定时样本，统计可信度低，停更仍必须告警。", "",
              "## 原始证据", "", "逐条提交SHA、数据日期、输出时间、可见时间、计划映射、Actions运行ID、排除原因和公式见同名JSON；可用固定source_ref和Actions快照离线复算。", ""]
    return "\n".join(lines)


def snapshot(root, sha, folder):
    names = git(root, "ls-tree", "-r", "--name-only", sha, "--", "data/市场状态", "data/社区情绪", "data/新闻", "data/信息源", "data/股票池", "data/v3", "logs/行情库_最新.md", "logs/v3_最新.md", "data/行情库/状态.csv").splitlines()
    if names:
        raw = git(root, "archive", sha, "--", *names, binary=True)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("历史文件路径无效")
                dest = folder / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(archive.extractfile(member).read())


def replay(root, manifest, stats, now, probe_time=None):
    commits = history(root, stats["source_ref"])
    rows, false_positive, unresolved = [], [], []
    for offset in range(13, -1, -1):
        date = now.date() - timedelta(days=offset)
        clocks = ["08:20", "22:30"]
        if probe_time and date == probe_time.date():
            clocks.append(probe_time.strftime("%H:%M"))
        for clock in sorted(set(clocks)):
            at = hc.scheduled(date, clock)
            if at > now:
                rows.append({"at": at.isoformat(), "coverage": "尚未来到，不回放未来"}); continue
            visible = [c for c in commits if c["at"] <= at]
            if not visible:
                rows.append({"at": at.isoformat(), "coverage": "仓库尚无历史，不伪造检查结果"}); continue
            sha = visible[-1]["sha"]
            with tempfile.TemporaryDirectory(prefix="T2-replay-") as tmp:
                folder = Path(tmp)
                snapshot(root, sha, folder)
                selected = copy.deepcopy(manifest)
                selected["items"] = [i for i in selected["items"] if i["id"] in stats["items"]
                                      and stats["items"][i["id"]]["introduced_at"]
                                      and hc.timestamp(stats["items"][i["id"]]["introduced_at"]) <= at]
                # 上线首轮没有可见的任何历史产出时，不用缺文件冒充定时失败。
                selected["items"] = [i for i in selected["items"] if any((folder / p.format(date=at.date().isoformat())).exists() for p in i["outputs"] if "{date}" not in p)
                                      or i["id"] == "community" and any((folder / "data/社区情绪").glob("????-??-??/运行记录.txt"))]
                for name in ("system/交易所休市.json", "data/行情库/指数/sh000300.csv"):
                    dest = folder / name; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes((root / name).read_bytes())
                (folder / "system/运行清单.json").write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
                report = hc.check(folder, at)
            alarms = []
            for item in report["items"]:
                if item["status"] == "正常":
                    continue
                label = "既有失败证据" if item["status"] in ("失败", "部分失败") else "产出缺失/过旧"
                if item["status"] == "延迟":
                    expected = item["expected"].get("data_date")
                    future = [s for s in stats["items"][item["id"]]["samples"] if hc.timestamp(s["available_at"]) > at]
                    if expected and any(s["data_date"] == expected for s in future):
                        label = "误报：历史后续正常到达"
                        false_positive.append({"at": at.isoformat(), "item": item["id"], "expected": expected})
                    elif item["id"] != "pool":
                        label = "未决：后续可观察历史不足"
                        unresolved.append({"at": at.isoformat(), "item": item["id"]})
                alarms.append({**item, "classification": label})
            rows.append({"at": at.isoformat(), "coverage": "按当时可见的Git快照检查；仅已上线且有首份产出的项", "sha": sha,
                         "checked_items": [i["id"] for i in report["items"]], "issues": alarms})
    return {"schema_version": 1, "checked_at": now.isoformat(), "source_ref": stats["source_ref"],
            "false_positive": false_positive, "unresolved": unresolved, "checks": rows,
            "scope": "固定本次28天校准参数的样本内回放；未覆盖日期、未上线任务及未来检查单列，不称完成14天独立验证"}


def render_replay(data):
    lines = ["# 健康检查历史回放", "", data["scope"], "", f"历史中可识别的延迟误报：{len(data['false_positive'])}；未决延迟：{len(data['unresolved'])}。", "",
             "| 检查时刻（北京时间） | 会开/更新的 Issue（日期+项目，同日复用） | 覆盖情况 |", "|---|---|---|"]
    for row in data["checks"]:
        alarms = "；".join(i["name"] + "：" + i["status"] + "（" + i["classification"] + "）" for i in row.get("issues", [])) or ("无" if row.get("checked_items") else "不计入误报分母")
        lines.append(f"| {row['at'][:16].replace('T', ' ')} | {alarms} | {row['coverage']} |")
    lines += ["", "原始快照SHA、预期与实际日期、具体失败标的/原因见同名JSON。501与股票池停更保留告警；没有通过统一填“正常”消除真实失败。", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--now")
    ap.add_argument("--actions", type=Path, default=Path("logs/test/T2/延迟校准/Actions.json"))
    ap.add_argument("--collect-actions", action="store_true")
    ap.add_argument("--apply-manifest", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--probe-time", help="额外核对一个历史检查时刻")
    args = ap.parse_args()
    root = args.root.resolve(); now = hc.timestamp(args.now) if args.now else datetime.now(hc.BJ)
    actions = args.actions if args.actions.is_absolute() else root / args.actions
    raw = collect_actions(root, now, actions) if args.collect_actions else json.loads(actions.read_text(encoding="utf-8"))
    path = root / "system/运行清单.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    stats = analyze(root, args.ref, manifest, raw, now)
    updated = calibrated(manifest, stats)
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs/运行延迟统计.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "logs/运行延迟统计.md").write_text(render(stats), encoding="utf-8")
    if args.apply_manifest:
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.replay:
        result = replay(root, updated, stats, now, hc.timestamp(args.probe_time) if args.probe_time else None)
        (root / "logs/test/T2/延迟校准").mkdir(parents=True, exist_ok=True)
        base = root / "logs/test/T2/延迟校准/历史回放"
        base.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        base.with_suffix(".md").write_text(render_replay(result), encoding="utf-8")
        if result["false_positive"]:
            raise RuntimeError(f"回放仍有 {len(result['false_positive'])} 项可识别延迟误报，拒绝通过")
    print(json.dumps({k: {"delay": v["delay"], "grace": v["grace"], "freshness": v["freshness"]} for k, v in stats["items"].items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
