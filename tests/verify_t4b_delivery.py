"""用已存真实档案验证正式入口；仅在临时副本落档，不触碰正式数据。"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import auto_judgments as generate
import auto_review as review
from auto_judgment_common import BJ, atomic_text, load_documents


def main():
    original = {str(p.relative_to(ROOT)): p.read_bytes() for p in (ROOT / "judgments").glob("*.jsonl")}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path in ("system", "judgments", "data/市场状态", "data/社区情绪", "data/行情库/指数", "data/行情库/ETF"):
            shutil.copytree(ROOT / path, root / path)
        day = json.loads((root / "data/市场状态/最新.json").read_text())["数据日期"]
        # 固定到档案当晚，重跑不受执行日变化影响；这不是正式历史补造。
        now = dt.datetime.fromisoformat(day + "T18:00:00").replace(tzinfo=BJ)
        first = generate.run(root, "市场状态", now=now)
        assert first["status"] == "正常", first["errors"]
        _, records, _ = load_documents(root / "judgments")
        additions = [r for _, _, r in records.values() if r.get("auto")]
        assert additions and all(r["confidence"] == "低" for r in additions)
        allowed = {"进入守势后的回撤风险·v1", "行业成交占比上升前5相对强弱·v1"}
        assert all(r["rule"] in allowed for r in additions)
        before = {p: p.read_bytes() for p in (root / "judgments").glob("*.jsonl")}
        again = generate.run(root, "市场状态", now=now + dt.timedelta(days=1))
        assert again["status"] == "正常"
        assert before == {p: p.read_bytes() for p in (root / "judgments").glob("*.jsonl")}
        review.run(root, now=now)
        for path, raw in original.items():
            assert (root / path).read_bytes().startswith(raw), path
        proof = {"数据日期": day, "隔离正式入口": first["status"], "新增规则": [r["rule"] for r in additions],
                 "新增条数": len(additions), "同事件跨日幂等": True, "手写原文逐字保护": True,
                 "正式档案SHA256": {p: hashlib.sha256(raw).hexdigest() for p, raw in original.items()}}
    assert original == {str(p.relative_to(ROOT)): p.read_bytes() for p in (ROOT / "judgments").glob("*.jsonl")}
    atomic_text(ROOT / "logs/test/T4b/交付验证.json", json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    main()
