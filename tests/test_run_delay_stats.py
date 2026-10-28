"""校准统计与历史取证边界，不联网。"""
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_delay_stats as stats


class DelayStatsTests(unittest.TestCase):
    def test_nearest_rank_does_not_underestimate_small_tail(self):
        self.assertEqual(stats.quantiles([1, 2, 3, 400]), {"n": 4, "p50": 2, "p90": 400, "p95": 400, "max": 400})
        self.assertIsNone(stats.allowance(stats.quantiles([])))

    def test_margin_is_measured_and_covers_observed_maximum(self):
        value = stats.allowance({"n": 100, "p50": 20, "p95": 40, "max": 90})
        self.assertEqual(value, {"p95_minutes": 40, "margin_minutes": 50, "minutes": 90})
        self.assertEqual(stats.allowance(stats.quantiles([3]))["minutes"], 4)

    def test_only_successful_scheduled_run_calibrates(self):
        at = stats.hc.timestamp("2026-10-28T23:30:00+08:00")
        base = {"path": "x", "event": "schedule", "status": "completed", "conclusion": "success",
                "created_at": "2026-10-28T22:00:00+08:00", "updated_at": "2026-10-28T23:31:00+08:00"}
        self.assertIsNone(stats.run_for({"at": at}, {"workflow": "x"}, [dict(base, event="workflow_dispatch"), dict(base, conclusion="failure")]))
        self.assertEqual(stats.run_for({"at": at}, {"workflow": "x"}, [base]), base)

    def test_calibrated_modes_use_measured_success_gap(self):
        row = {"delay": stats.quantiles([100, 200]), "grace": stats.allowance(stats.quantiles([100, 200])),
               "rotation_grace": None, "success_gap": stats.quantiles([300, 600]),
               "freshness": stats.allowance(stats.quantiles([300, 600])), "gap_definition": "实测成功间隔"}
        report = {"checked_at": "2026-10-29T07:00:00+08:00", "source_ref": "a" * 40, "formula": "公式",
                  "items": {"news": copy.deepcopy(row), "pool": copy.deepcopy(row)}}
        manifest = {"items": [{"id": "news"}, {"id": "pool", "trading_days_only": False}]}
        calibrated = stats.calibrated(manifest, report)
        news, pool = calibrated["items"]
        self.assertEqual(news["check_mode"], "freshness")
        self.assertEqual(news["max_age_minutes"], 900)
        self.assertEqual(news["delay_basis"]["freshness"]["p95_minutes"], 600)
        self.assertEqual(pool["check_mode"], "previous_trading_day")
        self.assertTrue(pool["trading_days_only"])
        self.assertNotIn("check_mode", manifest["items"][0])

    def test_no_samples_are_not_presented_as_measured_p95(self):
        known = {"delay": stats.quantiles([400]), "grace": stats.allowance(stats.quantiles([400])), "rotation_grace": None}
        missing = {"delay": stats.quantiles([]), "grace": None, "rotation_grace": None}
        report = {"checked_at": "2026-10-29T07:00:00+08:00", "source_ref": "b" * 40, "formula": "公式", "items": {"pool": known, "v3": missing}}
        out = stats.calibrated({"items": [{"id": "pool"}, {"id": "v3"}]}, report)["items"][1]
        self.assertEqual(out["grace_minutes"], 401)
        self.assertIsNone(out["delay_basis"]["p95_minutes"])
        self.assertIn("无本项定时样本", out["delay_basis"]["fallback"])

    def test_git_snapshot_handles_chinese_paths_and_preserves_exact_bytes(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            root = Path(source)
            subprocess.run(["git", "init", "-q", source], check=True)
            path = root / "logs/行情库_最新.md"; path.parent.mkdir(); path.write_text("测试快照\n", encoding="utf-8")
            subprocess.run(["git", "-C", source, "add", "."], check=True)
            subprocess.run(["git", "-C", source, "-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "-qm", "测试提交"], check=True)
            stats.snapshot(root, "HEAD", Path(target))
            self.assertEqual((Path(target) / "logs/行情库_最新.md").read_bytes(), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
