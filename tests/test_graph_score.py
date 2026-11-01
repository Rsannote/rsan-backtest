"""T7 驱动图谱打分：阈值从图谱读取、命令行只做临时覆盖、计划状态标注停更、每日运行登记。"""
import copy
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import graph_score  # noqa: E402
import health_check as health  # noqa: E402

GRAPH = {
    "version": "测试", "asOf": "2026-10-01",
    "rules": {"vote": "公司与某个驱动家族的连线强度≥2时，该家族记1票；同一家族内的多个驱动只算1票",
              "state_weights": {"加速": 1.5, "强": 1}, "pricing_drawdown": 0.2},
    "families": {"AI": "AI", "GRID": "电网"},
    "drivers": [{"id": "D1", "family": "AI", "state": "加速"}, {"id": "D3", "family": "GRID", "state": "强"}],
    "transmission": [{"id": "T1", "from": ["D1"]}],
    "companies": [
        {"code": "000001", "name": "深跌双票", "node": "x", "links": [["T1", 2, "1季"], ["D3", 3, "1季"]]},
        {"code": "000002", "name": "浅跌双票", "node": "x", "links": [["D1", 2, "1季"], ["D3", 2, "1季"]]},
        {"code": "000003", "name": "弱连单票", "node": "x", "links": [["D1", 1, "1季"], ["D3", 2, "1季"]]},
    ],
}


def days():
    d, out = dt.date(2025, 9, 1), []
    while d <= dt.date(2026, 10, 30):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class GraphScoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write_graph(GRAPH)
        ds = days()
        self.csv("data/行情库/指数/sh000300.csv", "date,close,factor", [f"{d},1,1" for d in ds])
        for code, last in (("000001", 50), ("000002", 90), ("000003", 10)):
            rows = [f"{d},100,100,100,100,1,1" for d in ds[:-1]] + [f"{ds[-1]},{last},{last},{last},{last},1,1"]
            self.csv(f"data/行情库/股票/{code}.csv", "date,open,high,low,close,volume,factor", rows)
        self.csv("data/股票池/股票.csv", "code,name,l1_status,plan_status,plan_priceDate",
                 ["000001,深跌双票,通过,接近,2026-10-12", "000002,浅跌双票,通过,等待,2026-10-12"])
        (self.root / "data/股票池/状态.json").write_text(json.dumps({"数据日期": "2026-10-28"}), encoding="utf-8")

    def write_graph(self, graph, name="驱动图谱_v0.json"):
        p = self.root / "graph" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    def csv(self, path, header, rows):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    def by_code(self, report):
        return {r["code"]: r for r in report["companies"]}

    def test_threshold_and_weights_come_from_graph(self):
        report = graph_score.build(self.root)
        self.assertEqual(report["rules"]["drawdown_threshold"], 0.2)
        self.assertEqual(report["rules"]["drawdown_threshold_source"], "图谱rules")
        self.assertEqual(report["rules"]["state_weights_source"], "图谱rules")
        rows = self.by_code(report)
        self.assertEqual([rows[c]["votes"] for c in ("000001", "000002", "000003")], [2, 2, 1])
        self.assertIs(rows["000001"]["cross_candidate"], True)    # 回撤50% ≥ 20%
        self.assertIs(rows["000002"]["cross_candidate"], False)   # 回撤10%
        self.assertIsNone(rows["000003"]["cross_candidate"])      # 不在股票池，计划状态缺失，不判
        self.assertEqual(rows["000001"]["weighted_score"], 2 * 1.5 + 3 * 1)

    def test_command_line_overrides_only_temporarily(self):
        report = graph_score.build(self.root, drawdown_threshold=0.6)
        self.assertEqual(report["rules"]["drawdown_threshold_source"], "命令行临时覆盖")
        self.assertIs(self.by_code(report)["000001"]["cross_candidate"], False)
        self.assertEqual(json.loads((self.root / "graph/驱动图谱_v0.json").read_text(encoding="utf-8"))["rules"]["pricing_drawdown"], 0.2)

    def test_missing_invalid_or_conflicting_threshold(self):
        g = copy.deepcopy(GRAPH)
        del g["rules"]["pricing_drawdown"]
        self.write_graph(g)
        report = graph_score.build(self.root)
        self.assertIsNone(report["rules"]["drawdown_threshold"])
        self.assertIsNone(self.by_code(report)["000001"]["cross_candidate"])
        g["rules"]["pricing_drawdown"] = 1.5
        self.write_graph(g)
        with self.assertRaises(ValueError):
            graph_score.build(self.root)
        g["rules"]["pricing_drawdown"] = 0.3
        self.write_graph(GRAPH)
        self.write_graph(dict(g, companies=[]), "驱动图谱_v1.json")
        with self.assertRaisesRegex(ValueError, "回撤阈值冲突"):
            graph_score.build(self.root)

    def test_pool_snapshot_marked_stale(self):
        report = graph_score.build(self.root)
        self.assertTrue(any("不再每日镜像" in w for w in report["warnings"]))
        self.assertEqual(self.by_code(report)["000001"]["pool"]["snapshot_date"], "2026-10-28")
        md = graph_score.render(report)
        self.assertIn("计划状态（快照，停更）", md)
        self.assertIn("股票池快照 2026-10-28，**已停更每日镜像**", md)
        self.assertIn("回撤≥0.2（图谱rules）", md)

    def test_cli_writes_outputs_without_flags(self):
        out = self.root / "out"
        done = subprocess.run([sys.executable, str(ROOT / "scripts/graph_score.py"), "--root", str(self.root), "--out-dir", str(out)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads((out / "打分_最新.json").read_text(encoding="utf-8"))
        self.assertEqual(data["as_of"], "2026-10-30")
        self.assertIn("回撤阈值：0.2（图谱rules）", done.stdout)

    def test_health_check_sees_daily_output(self):
        manifest = health.load_manifest(ROOT)
        item = next(i for i in manifest["items"] if i["id"] == "graph_score")
        out = self.root / "graph"
        report = graph_score.build(self.root)
        (out / "打分_最新.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        (out / "打分_最新.md").write_text(graph_score.render(report), encoding="utf-8")
        (self.root / "system").mkdir(exist_ok=True)
        (self.root / "system/交易所休市.json").write_bytes((ROOT / "system/交易所休市.json").read_bytes())
        cal = health.Calendar(self.root, {"history": "data/行情库/指数/sh000300.csv", "closures": "system/交易所休市.json"})
        ok = health.check_item(self.root, item, health.timestamp("2026-10-31T08:20:00+08:00"), cal)
        self.assertEqual(ok["status"], "正常", ok)
        late = health.check_item(self.root, item, health.timestamp("2026-11-03T08:20:00+08:00"), cal)
        self.assertEqual(late["status"], "延迟", late)


class RealGraphTests(unittest.TestCase):
    def test_real_graph_votes_unchanged(self):
        """真实图谱的票数与 T7 审核时的手算核对一致（18 家，票数≥2 的 5 家）。"""
        report = graph_score.build(ROOT)
        votes = {r["code"]: r["votes"] for r in report["companies"]}
        self.assertEqual(len(votes), 18)
        self.assertEqual({c for c, v in votes.items() if v >= 2}, {"002028", "600941", "600406", "002270", "601126"})
        self.assertEqual(votes["002028"], 3)
        self.assertEqual(report["rules"]["drawdown_threshold"], 0.2)

    def test_workflow_runs_daily_after_market_state(self):
        s = (ROOT / ".github/workflows/graph-score.yml").read_text(encoding="utf-8")
        self.assertIn("workflows: ['市场状态']", s)
        self.assertIn("python -u scripts/graph_score.py\n", s)
        self.assertNotIn("--drawdown-threshold", s)


if __name__ == "__main__":
    unittest.main()
