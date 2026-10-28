#!/usr/bin/env python3
"""T1 复审：隔离目录重跑四只 ETF，原始全库验收证据另行保留。"""
import ast
import csv
import hashlib
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import verify_t1_etf as verify


def hashes():
    return {str(p.relative_to(verify.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (verify.ROOT / "data/行情库").rglob("*") if p.is_file()}


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", "联网验收只能在 GitHub Actions 执行"
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    verify.OUT = verify.ROOT / "logs/test/T1" / ("复审_" + now[:10])
    verify.OUT.mkdir(parents=True, exist_ok=True)
    original = subprocess.check_output(["git", "show", "40557a79a79e5fd02c49b09632d4850c80c07bda:scripts/update_hangqing.py"], cwd=verify.ROOT, text=True)
    old_ast = ast.parse(original)
    new_ast = ast.parse((verify.ROOT / "scripts/update_hangqing.py").read_text(encoding="utf-8"))
    for name in ("factors", "write_store", "drop_spikes"):
        old = next(n for n in old_ast.body if isinstance(n, ast.FunctionDef) and n.name == name)
        new = next(n for n in new_ast.body if isinstance(n, ast.FunctionDef) and n.name == name)
        assert ast.dump(old) == ast.dump(new), name
    before = hashes()
    with tempfile.TemporaryDirectory(prefix="T1-review-") as temp:
        target = verify.sandbox(Path(temp), "复审四只ETF")
        first = {}
        for code in verify.CODES:
            path = target / f"data/行情库/ETF/{code}.csv"
            with path.open(encoding="utf-8") as f:
                first[code] = next(csv.DictReader(f))["date"]
            path.unlink()
        for args, filename in ((["--codes", ",".join(verify.CODES)], "首次建库.txt"),
                               (["--full", "--codes", ",".join(verify.CODES)], "全量重抓.txt"),
                               (["--codes", ",".join(verify.CODES)], "再次增量.txt")):
            states = verify.cli(target, args, filename)
            assert all(states[c]["first"] == first[c] for c in verify.CODES), "抓取首日与已存腾讯历史不一致"
        market = verify.load(target / "scripts/market_state.py", "market_review")
        day = market.read_hq("指数", "sh000300").index[-1]
        sector, warnings = market.sector_part(day)
        assert sector["行业数"] == 34, warnings
        lines = ["# T1 复审抓取验收", "", f"时间：{now}（北京时间）；代码：`{os.environ['GITHUB_SHA']}`。", "",
                 "首次建库、全量重抓、再次增量均成功 4、失败 0；首日逐一与原库核对一致。", "",
                 "| 代码 | 首日 | 末日 | 行数 |", "|---|---|---|---|"]
        lines += [f"| {c} | {states[c]['first']} | {states[c]['last']} | {states[c]['rows']} |" for c in verify.CODES]
        lines += ["", f"板块日期 {day}，行业数 {sector['行业数']}。", "复权、CSV 精度、坏点过滤的 AST 与原版一致。"]
    assert hashes() == before, "正式行情库发生变化"
    lines += ["正式行情库所有文件 SHA256 前后一致。", "", "本轮重点验证审核修改；10-22 全部 170 个其他标的的逐字节对比证据保留在上级目录，未覆盖。"]
    (verify.OUT / "验收.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
