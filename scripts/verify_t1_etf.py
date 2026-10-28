#!/usr/bin/env python3
"""T1 Actions 验收：真实抓取与同响应回放，所有正式数据只读。"""
import argparse
import ast
import contextlib
import csv
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs/test/T1"
CODES = ("512200", "516950", "159930", "159611")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sandbox(parent, name):
    dest = parent / name
    (dest / "data").mkdir(parents=True)
    shutil.copytree(ROOT / "scripts", dest / "scripts")
    shutil.copytree(ROOT / "data/行情库", dest / "data/行情库")
    shutil.copytree(ROOT / "data/市场状态", dest / "data/市场状态")
    shutil.copyfile(ROOT / "data/标的清单.csv", dest / "data/标的清单.csv")
    return dest


def cli(dest, args, filename):
    cmd = [sys.executable, "scripts/update_hangqing.py", *args]
    with (OUT / filename).open("w", encoding="utf-8") as log:
        log.write("命令：" + " ".join(cmd) + "\n目录：Actions 临时隔离目录\n")
        log.flush()
        result = subprocess.run(cmd, cwd=dest, stdout=log, stderr=subprocess.STDOUT)
    print((OUT / filename).read_text(encoding="utf-8"), flush=True)
    assert result.returncode == 0, f"抓取失败，见 {filename}"
    state = {r["code"]: r for r in csv.DictReader((dest / "data/行情库/状态.csv").open(encoding="utf-8"))}
    for code in CODES:
        assert code in state and state[code]["first"] and state[code]["last"], code
    # main 容忍少量失败，因此另查日志，不只检查退出码或旧状态行。
    report = (dest / "logs/行情库_最新.md").read_text(encoding="utf-8")
    assert "成功 4，失败 0，超时未轮到 0" in report, report
    return state


def run_main(module, get, filename):
    module.get = get
    previous = sys.argv
    sys.argv = ["scripts/update_hangqing.py"]
    try:
        with (OUT / filename).open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log):
            return module.main()
    finally:
        sys.argv = previous


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="修改前提交 SHA")
    args = parser.parse_args()
    assert os.environ.get("GITHUB_ACTIONS") == "true", "联网验收只能在 GitHub Actions 执行"
    OUT.mkdir(parents=True, exist_ok=True)
    summary = ["# T1 分支验收", "", f"修改前提交：`{args.base}`。正式 data/ 只读；抓取及计算在临时目录执行。", ""]
    original = subprocess.check_output(["git", "show", f"{args.base}:scripts/update_hangqing.py"], cwd=ROOT, text=True)
    old_ast = ast.parse(original)
    new_ast = ast.parse((ROOT / "scripts/update_hangqing.py").read_text(encoding="utf-8"))
    for name in ("factors", "write_store", "drop_spikes"):
        before = next(n for n in old_ast.body if isinstance(n, ast.FunctionDef) and n.name == name)
        after = next(n for n in new_ast.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert ast.dump(before) == ast.dump(after), name
    summary.append("复权算法、CSV 写出精度和坏点规则的 AST 与修改前一致。")
    with tempfile.TemporaryDirectory(prefix="T1-") as temp:
        parent = Path(temp)
        target = sandbox(parent, "四只ETF")
        market = load(target / "scripts/market_state.py", "market_target")
        day = market.read_hq("指数", "sh000300").index[-1]
        for code in CODES:
            (target / f"data/行情库/ETF/{code}.csv").unlink(missing_ok=True)
        reduced, _ = market.sector_part(day)
        # 清掉状态行，防止旧状态掩盖首次建库失败。
        state_path = target / "data/行情库/状态.csv"
        rows = list(csv.DictReader(state_path.open(encoding="utf-8")))
        with state_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(r for r in rows if r["code"] not in CODES)
        states = cli(target, ["--codes", ",".join(CODES)], "四只ETF_首次建库.txt")
        states = cli(target, ["--full", "--codes", ",".join(CODES)], "四只ETF_全量重抓.txt")
        cli(target, ["--codes", ",".join(CODES)], "四只ETF_再次增量.txt")
        summary += ["", "## 四只 ETF", "", "| 代码 | 首日 | 末日 | 行数 |", "|---|---|---|---|"]
        for code in CODES:
            row = states[code]
            summary.append(f"| {code} | {row['first']} | {row['last']} | {row['rows']} |")
        shutil.copyfile(state_path, OUT / "四只ETF_状态.csv")
        complete, warnings = market.sector_part(day)
        assert reduced["行业数"] == 30 and complete["行业数"] == 34, (reduced["行业数"], complete["行业数"], warnings)
        summary += ["", f"板块计算日期 {day}：模拟缺少这 4 只时为 {reduced['行业数']}，真实抓取后为 {complete['行业数']}。主分支开工前已为 34，本项复现缺失到补齐的行为。"]
        # 跑实际入口，指定日期避免访问实时指标接口。
        result = subprocess.run([sys.executable, "scripts/market_state.py", "--date", day], cwd=target, capture_output=True, text=True)
        (OUT / "市场状态.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0
        generated = json.loads((target / "data/市场状态/最新.json").read_text(encoding="utf-8"))
        assert generated["板块"]["行业数"] == 34
        summary += ["", "## 全部标的增量回归", "", "修改前真实联网抓取一次，记录每个 URL 的原始成功响应或异常；修改后从相同数据副本回放同一响应，以排除源站变化。两次均调用不带参数的 main()，即全部标的增量入口。"]
        before = sandbox(parent, "修改前")
        after = sandbox(parent, "修改后")
        old_path = before / "scripts/update_hangqing.py"
        old_path.write_text(original, encoding="utf-8")
        old = load(old_path, "hq_before")
        new = load(after / "scripts/update_hangqing.py", "hq_after")
        real_get = old.get
        cache, offsets, missing, lock = {}, {}, [], threading.Lock()

        def capture(url, headers, *pos, **kwargs):
            try:
                value = real_get(url, headers, *pos, **kwargs)
                error = None
            except Exception as exc:
                value, error = None, exc
            with lock:
                cache.setdefault(url, []).append((value, error))
            if error is not None:
                raise error
            return value

        def replay(url, headers, *pos, **kwargs):
            with lock:
                index = offsets.get(url, 0)
                if url not in cache or index >= len(cache[url]):
                    missing.append(url)
                    raise AssertionError(f"回放未命中：{url}")
                offsets[url] = index + 1
                value, error = cache[url][index]
            if error is not None:
                raise error
            return value

        print("开始全部标的增量：修改前真实抓取，修改后相同响应回放。", flush=True)
        old_rc = run_main(old, capture, "全部增量_修改前.txt")
        new_rc = run_main(new, replay, "全部增量_修改后.txt")
        assert not missing, missing
        assert all(offsets.get(url, 0) == len(values) for url, values in cache.items()), "有原始响应未被回放"
        assert old_rc == new_rc, (old_rc, new_rc)
        baseline = {r["code"]: r for r in csv.DictReader((before / "data/行情库/状态.csv").open(encoding="utf-8"))}
        current = {r["code"]: r for r in csv.DictReader((after / "data/行情库/状态.csv").open(encoding="utf-8"))}
        fields = [k for k in next(iter(baseline.values())) if k != "updated"]
        items = list(csv.DictReader((ROOT / "data/标的清单.csv").open(encoding="utf-8")))
        compared = total = 0
        differences = []
        for item in items:
            code = item["code"]
            if code in CODES:
                continue
            compared += 1
            if {k: baseline.get(code, {}).get(k) for k in fields} != {k: current.get(code, {}).get(k) for k in fields}:
                differences.append(f"{code} 状态内容不同")
            for rel in (f"data/行情库/{item['type']}/{code}.csv", f"data/行情库/分红表/{code}.json"):
                a, b = before / rel, after / rel
                if a.exists() != b.exists() or (a.exists() and a.read_bytes() != b.read_bytes()):
                    differences.append(rel)
                elif a.exists() and a.suffix == ".csv":
                    total += len(a.read_text(encoding="utf-8").splitlines()) - 1
        (OUT / "增量差异.json").write_text(json.dumps(differences, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert not differences, differences
        summary += ["", "对比：逐文件 read_bytes() 比较原始 CSV（比逐行更严格），同时比较分红 JSON、去掉 updated 的状态字段；4 只目标 ETF 不计入其他标的。", "", f"其他标的 {compared} 个，共 {total} 行；CSV/分红表/状态差异 {len(differences)}。", f"原始响应 URL 数 {len(cache)}；两版退出码 {old_rc}/{new_rc}。接口失败明细保留在两份全部增量日志，失败不等于成功。"]
    summary += ["", "验收仅产生 logs/test/T1/ 证据；不提交正式行情、市场状态及回测数据。"]
    (OUT / "验收.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary), flush=True)


if __name__ == "__main__":
    main()
