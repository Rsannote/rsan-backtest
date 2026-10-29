#!/usr/bin/env python3
"""仅在Actions从最新main复制隔离库验收T11；正式目录只写logs/test/T11。"""
import argparse
import ast
import contextlib
import csv
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import health_check as hc

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs/test/T11"
DEFAULT_CODES = "sh000300,600872,600874,600875,600887"


def command(args, cwd=None, **kwargs):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, **kwargs)


def protected_hashes():
    paths = []
    for name in ("data", "engine", "archive", "judgments"):
        paths.extend(path for path in (ROOT / name).rglob("*") if path.is_file())
    paths += [ROOT / name for name in ("system/运行清单.json", "logs/运行延迟统计.json", "logs/运行延迟统计.md") if (ROOT / name).is_file()]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def critical_ast(source_sha):
    old = ast.parse(command(["git", "show", source_sha + ":scripts/update_hangqing.py"], ROOT).stdout)
    new = ast.parse((ROOT / "scripts/update_hangqing.py").read_text(encoding="utf-8"))
    names = ("update_one", "factors", "write_store", "drop_spikes", "fetch_full", "fetch_raw", "fetch_segment", "get")
    for name in names:
        find = lambda tree: next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        if ast.dump(find(old)) != ast.dump(find(new)):
            raise AssertionError(f"原单标的/T1函数变化：{name}")
    return list(names)


def run_update(sandbox, args, targets):
    updater = sandbox / "scripts/update_hangqing.py"
    options = [] if args.all else ["--codes", ",".join(targets)]
    if args.full:
        options += ["--full"]
    if args.rotate:
        options += ["--rotate"]
    if args.inject_501:
        hq = load(updater, "t11_injected_updater")
        original = hq.update_one
        seen = set()
        def injected(item, full):
            if item["code"] == args.inject_501 and item["code"] not in seen:
                seen.add(item["code"])
                return dict(item, status="失败", notes=["故障注入：模拟 HTTPError 501，仅主循环一次；补抓子进程仍请求真实源"])
            return original(item, full)
        hq.update_one = injected
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = hq.main(options)
        return code, stream.getvalue(), ""
    result = subprocess.run([sys.executable, str(updater), *options], cwd=sandbox, capture_output=True, text=True, timeout=80 * 60)
    return result.returncode, result.stdout, result.stderr


def delay_rerun(sandbox, source_sha, evidence):
    """统计在有完整Git历史的隔离副本重跑，既不apply也不改正式stats。"""
    manifest_path = sandbox / "system/运行清单.json"
    original = manifest_path.read_bytes()
    raw_actions = Path("logs/test/T11/Actions_重采.json")
    try:
        result = command([sys.executable, "scripts/run_delay_stats.py", "--root", str(sandbox), "--ref", source_sha,
                          "--collect-actions", "--actions", str(raw_actions)], sandbox)
    except subprocess.CalledProcessError as exc:
        (evidence / "延迟统计_执行.txt").write_text((exc.stdout or "") + (exc.stderr or ""), encoding="utf-8")
        raise
    finally:
        if (sandbox / raw_actions).is_file():
            shutil.copyfile(sandbox / raw_actions, evidence / "Actions_隔离重采.json")
    (evidence / "延迟统计_执行.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    for suffix in ("json", "md"):
        shutil.copyfile(sandbox / f"logs/运行延迟统计.{suffix}", evidence / f"运行延迟统计_隔离重跑.{suffix}")
    module = load(sandbox / "scripts/run_delay_stats.py", "t11_delay_rerun")
    manifest = json.loads(original)
    stats = json.loads((sandbox / "logs/运行延迟统计.json").read_text())
    proposed = module.calibrated(manifest, stats)
    pool_before = next(i for i in manifest["items"] if i["id"] == "pool")
    pool_after = next(i for i in proposed["items"] if i["id"] == "pool")
    assert pool_before == pool_after and pool_after["check_mode"] == "freshness" and pool_after["max_age_minutes"] == 144000
    assert manifest_path.read_bytes() == original
    try:
        module.require_four_weeks(manifest, stats)
        ready, reason = True, "四周样本保护已满足；本助手仍不应用任何参数"
    except ValueError as exc:
        ready, reason = False, str(exc)
    return {"可复跑": True, "清单原字节未变": True, "股票池政策": "freshness=144000分钟（100天），整项保留", "允许正式应用": ready, "原因": reason}


def consecutive_days(dates, calendar):
    streak, longest, previous = 0, 0, None
    for value in sorted(set(dates)):
        current = hc.day(value)
        contiguous = previous is None
        if previous is not None:
            probe = previous + hc.dt.timedelta(days=1)
            contiguous = True
            while probe < current:
                if calendar.is_open(probe) is not False:
                    contiguous = False
                    break
                probe += hc.dt.timedelta(days=1)
        streak = streak + 1 if contiguous else 1
        longest, previous = max(longest, streak), current
    return longest


def implementation_fingerprint():
    files = {"scripts/" + name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
             for name in ("update_hangqing.py", "verify_t11_retry.py", "run_delay_stats.py")}
    combined = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"sha256": combined, "files": files}


def qualified_dates(records, fingerprint):
    """仅同一实现累积；文档提交不重置，同日重跑不重复，旧版证据不混算。"""
    return sorted({record["交易数据日期"] for record in records
                   if record.get("计入完整真实交易日验收") and record.get("验收实现指纹") == fingerprint})


def acceptance_day(proof, args, calendar, started, finished):
    reasons = []
    if not args.all:
        reasons.append("仅小样本")
    if args.inject_501:
        reasons.append("故障注入")
    if proof["退出码"] != 0 or proof["最终失败与原因"]:
        reasons.append("仍有真实失败，须审核原因，不自动算通过")
    if calendar.is_open(started.date()) is not True:
        reasons.append("执行日不是已确认交易日")
    if started.hour < 15 or finished.date() != started.date():
        reasons.append("未在执行交易日收盘后开始并于当日完成")
    if any(value != started.date().isoformat() for value in proof["关键标的日期"].values()):
        reasons.append("关键标的或沪深300的实际数据日期尚未到执行日")
    if proof["实际增加耗时秒"] > 600:
        reasons.append("补抓增加耗时超过600秒")
    return not reasons, reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-ref", default="origin/main", help="CI先git fetch main；只读使用该引用的最新行情副本")
    ap.add_argument("--codes", default=DEFAULT_CODES)
    ap.add_argument("--all", action="store_true", help="完整标的清单验收；小样本烟雾不计入完整3日验收")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--inject-501", help="只在主循环给指定标的注入一次501；不计入3个真实交易日")
    ap.add_argument("--stats-only", action="store_true", help="仅在隔离副本复跑统计，不发行情请求")
    args = ap.parse_args()
    assert os.environ.get("GITHUB_ACTIONS") == "true", "联网验收只能在GitHub Actions执行"
    before = protected_hashes()
    now = datetime.now(hc.BJ)
    source_sha = command(["git", "rev-parse", "--verify", args.source_ref + "^{commit}"], ROOT).stdout.strip()
    execution = os.environ.get("GITHUB_RUN_ID", now.strftime("%H%M%S")) + "_" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    execution += "_" + ("统计" if args.stats_only else "注入" if args.inject_501 else "全库" if args.all else "烟雾")
    evidence = OUT / "执行" / (now.strftime("%Y-%m-%d_") + execution)
    evidence.mkdir(parents=True, exist_ok=True)
    proof = {"执行时间": now.isoformat(), "执行日期": now.date().isoformat(), "来源引用": args.source_ref, "来源提交": source_sha,
             "分支代码": os.environ.get("GITHUB_SHA"), "故障注入": args.inject_501, "全量标的验收": args.all,
             "抓取模式": {"full": args.full, "rotate": args.rotate},
             "验收实现指纹": implementation_fingerprint(),
             "说明": "最新main只读clone到隔离目录；不复制隔离产出到正式data；同一数据日期只计一次。"}
    error = None
    try:
        proof["原逻辑AST一致"] = critical_ast(source_sha)
        with tempfile.TemporaryDirectory(prefix="T11-verify-") as temp:
            sandbox = Path(temp) / "repo"
            command(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(sandbox)])
            command(["git", "checkout", "--detach", source_sha], sandbox)
            for name in ("update_hangqing.py", "run_delay_stats.py"):
                shutil.copyfile(ROOT / "scripts" / name, sandbox / "scripts" / name)
            proof["统计安全复跑"] = delay_rerun(sandbox, source_sha, evidence)
            if not args.stats_only:
                with (sandbox / "data/标的清单.csv").open(encoding="utf-8") as file:
                    items = list(csv.DictReader(file))
                targets = [i["code"] for i in items] if args.all else list(dict.fromkeys(args.codes.split(",") + ["sh000300"]))
                critical = list(dict.fromkeys(args.codes.split(",") + ["sh000300"]))
                assert not set(targets) - {i["code"] for i in items}, "目标必须在来源清单内"
                assert not set(critical) - set(targets), "关键标的必须在本次完整范围内"
                assert not args.inject_501 or args.inject_501 in targets, "注入标的必须在本次范围内"
                code, stdout, stderr = run_update(sandbox, args, targets)
                (evidence / "行情执行.txt").write_text(stdout + stderr, encoding="utf-8")
                report = (sandbox / "logs/行情库_最新.md").read_text(encoding="utf-8")
                (evidence / "行情库_最新.md").write_text(report, encoding="utf-8")
                with (sandbox / "data/行情库/状态.csv").open(encoding="utf-8") as file:
                    states = {r["code"]: r for r in csv.DictReader(file)}
                proof["实际标的日期"] = {c: states.get(c, {}).get("last") for c in targets}
                proof["关键标的日期"] = {c: states.get(c, {}).get("last") for c in critical}
                proof["交易数据日期"] = states["sh000300"]["last"]
                match = re.search(r"主循环失败：(\d+)；补抓成功：(\d+)；补抓轮数：(\d+).*?额外用时 ([\d.]+) 秒", report)
                assert match, "缺少主循环/补抓统计"
                proof.update(主循环失败数=int(match[1]), 补抓成功数=int(match[2]), 补抓轮数=int(match[3]), 实际增加耗时秒=float(match[4]),
                             补抓成功名单=re.findall(r"^- (.+)$", report.split("## 补抓成功", 1)[1].split("## ", 1)[0], re.M) if "## 补抓成功" in report else [],
                             最终失败与原因=hc.fail_sections(report), 退出码=code)
                calendar = hc.Calendar(sandbox, {"history": "data/行情库/指数/sh000300.csv", "closures": "system/交易所休市.json"})
                finished = datetime.now(hc.BJ)
                eligible, reasons = acceptance_day(proof, args, calendar, now, finished)
                proof["计入完整真实交易日验收"] = eligible
                proof["不计入原因"] = reasons
                proof["计数说明"] = "仅完整范围、交易日收盘后开始、关键标的及沪深300的last到执行日、最终失败0且补抓≤600秒时计入；小样本/注入/上午烟雾均不计数。长期停牌的其他标的日期单列；仍失败但非限流的例外留给审核，不自动算通过。"
                previous = [json.loads(p.read_text()) for p in (OUT / "执行").glob("*/结果.json")]
                dates = qualified_dates(previous, proof["验收实现指纹"])
                if eligible:
                    dates.append(proof["交易数据日期"])
                proof["已积累真实交易日"] = sorted(set(dates))
                proof["连续真实交易日数"] = consecutive_days(dates, calendar)
                if not args.all or args.inject_501:
                    proof["验收范围"] = "烟雾/故障注入，不替代完整3日验收"
                if code != 0 or proof["最终失败与原因"]:
                    raise AssertionError("真实行情仍有失败，保留证据并使CI失败，非限流例外由审核决定")
                if args.inject_501 and not any(line.startswith(args.inject_501 + " ") for line in proof["补抓成功名单"]):
                    raise AssertionError("注入标的没有出现在补抓成功名单，故障恢复未验收")
    except Exception as exc:
        error = exc
        proof["验收错误"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, subprocess.CalledProcessError):
            proof["执行错误输出"] = ((exc.stdout or "") + (exc.stderr or ""))[-8000:]
    finally:
        after = protected_hashes()
        proof["正式保护哈希一致"] = before == after
        (evidence / "保护哈希.json").write_text(json.dumps({"前": before, "后": after}, ensure_ascii=False, indent=2), encoding="utf-8")
        (evidence / "结果.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (evidence / "结果.md").write_text("# T11隔离验收\n\n```json\n" + json.dumps(proof, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")
        assert proof["正式保护哈希一致"], "正式data/回测/清单或正式延迟统计发生变化"
    if error:
        raise error
    print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
