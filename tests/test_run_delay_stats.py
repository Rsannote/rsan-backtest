"""校准统计与历史取证边界，不联网。"""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_delay_stats as stats


class DelayStatsTests(unittest.TestCase):
    def test_rolling_window_does_not_reset_multislot_sequence_to_inception(self):
        before = stats.hc.timestamp("2026-09-29T09:00:00+08:00")
        inside = stats.hc.timestamp("2026-09-30T09:00:00+08:00")
        commits = [{"sha": str(i) * 40, "at": at, "author": "github-actions", "title": "新闻库 模拟"}
                   for i, at in enumerate((before, inside), 1)]
        item = {"id": "news", "name": "新闻", "workflow": "w", "times": ["08:00"]}
        with patch.object(stats, "git", return_value="a" * 40), patch.object(stats, "history", return_value=commits), \
             patch.object(stats, "introduced", return_value=before.replace(hour=0)), \
             patch.object(stats, "run_for", return_value={"id": 1}), \
             patch.object(stats, "produced", side_effect=lambda root, c, item: (c["at"].date().isoformat(), c["at"], True)):
            out = stats.analyze(Path("/tmp"), "ref", {"items": [item]}, {"runs": []},
                                stats.hc.timestamp("2026-10-28T08:30:00+08:00"))["items"]["news"]
        self.assertEqual(out["delay"]["n"], 1)
        self.assertEqual(out["delay"]["p95"], 60)
        self.assertEqual(len(out["samples"]), 1)
        self.assertEqual(out["samples"][0]["scheduled_at"], "2026-09-30T08:00:00+08:00")

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
        policy = {"id": "pool", "trading_days_only": False, "check_mode": "freshness", "max_age_minutes": 144000,
                  "freshness_success_paths": [{"path": "data/股票池/状态.json", "field": "最近检查"}], "说明": "审核批准100天"}
        manifest = {"items": [{"id": "news"}, policy]}
        calibrated = stats.calibrated(manifest, report)
        news, pool = calibrated["items"]
        self.assertEqual(news["check_mode"], "freshness")
        self.assertEqual(news["max_age_minutes"], 900)
        self.assertEqual(news["delay_basis"]["freshness"]["p95_minutes"], 600)
        self.assertEqual(pool, policy)
        self.assertNotIn("check_mode", manifest["items"][0])

    def test_no_samples_are_not_presented_as_measured_p95(self):
        known = {"delay": stats.quantiles([400]), "grace": stats.allowance(stats.quantiles([400])), "rotation_grace": None}
        missing = {"delay": stats.quantiles([]), "grace": None, "rotation_grace": None}
        report = {"checked_at": "2026-10-29T07:00:00+08:00", "source_ref": "b" * 40, "formula": "公式", "items": {"hangqing": known, "v3": missing}}
        out = stats.calibrated({"items": [{"id": "hangqing"}, {"id": "v3"}]}, report)["items"][1]
        self.assertEqual(out["grace_minutes"], 401)
        self.assertIsNone(out["delay_basis"]["p95_minutes"])
        self.assertIn("无本项定时样本", out["delay_basis"]["fallback"])

    def ready(self):
        return {"checked_at": "2026-11-29T08:20:00+08:00", "first_repository_commit": "2026-10-20T00:00:00+08:00",
                "items": {"hangqing": {"introduced_at": "2026-10-21T00:00:00+08:00", "first_success_at": "2026-10-22T23:30:00+08:00",
                                       "samples": [{"success": True, "available_at": date + "T23:30:00+08:00"}
                                                   for date in ("2026-11-03", "2026-11-10", "2026-11-17", "2026-11-24")]}}}

    def test_actual_four_weeks_allows_apply_without_impossible_window_span(self):
        report = self.ready()
        # 四段样本只跨21天，完整历史已经证明首成功在窗口前；仍应通过。
        stats.require_four_weeks({"items": [{"id": "hangqing"}]}, report)

    def test_old_repository_alone_does_not_prove_task_or_success_coverage(self):
        for field in ("introduced_at", "first_success_at"):
            report = self.ready()
            report["items"]["hangqing"][field] = "2026-11-02T00:00:00+08:00"
            with self.subTest(field=field), self.assertRaises(ValueError):
                stats.require_four_weeks({"items": [{"id": "hangqing"}]}, report)
        report = self.ready()
        report["items"]["hangqing"]["samples"].pop()
        with self.assertRaises(ValueError):
            stats.require_four_weeks({"items": [{"id": "hangqing"}]}, report)

    def test_sparse_selection_and_manual_policy_are_not_applied(self):
        original = {"items": [{"id": "v3", "grace_minutes": 400}, {"id": "pool", "check_mode": "freshness", "max_age_minutes": 144000},
                              {"id": "hangqing", "grace_minutes": 400}]}
        proposal = copy.deepcopy(original)
        for item in proposal["items"]:
            item["grace_minutes"] = 999
        applied = stats.applicable(original, proposal)
        self.assertEqual(applied["items"][:2], original["items"][:2])
        self.assertEqual(applied["items"][2]["grace_minutes"], 999)

    def test_apply_rejects_short_history_without_changing_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "system/运行清单.json"; path.parent.mkdir()
            original = b'{"items": [{"id": "pool", "max_age_minutes": 144000}]}\n'
            path.write_bytes(original)
            actions = root / "actions.json"; actions.write_text('{"runs": []}')
            short = {"checked_at": "2026-10-29T08:20:00+08:00", "first_repository_commit": "2026-10-20T00:00:00+08:00", "items": {}}
            with patch.object(stats, "analyze", return_value=short), patch.object(stats, "calibrated", return_value={"items": []}), \
                 patch("sys.argv", ["stats", "--root", folder, "--actions", str(actions), "--apply-manifest"]):
                with self.assertRaisesRegex(ValueError, "不足28天"):
                    stats.main()
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "logs/运行延迟统计.json").exists())

    def test_cli_after_four_weeks_applies_daily_grace_and_retains_pool(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "system/运行清单.json"; path.parent.mkdir()
            policy = {"id": "pool", "check_mode": "freshness", "max_age_minutes": 144000, "说明": "审核批准100天"}
            manifest = {"items": [{"id": "hangqing", "grace_minutes": 999}, policy, {"id": "v3", "grace_minutes": 999}]}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            actions = root / "actions.json"; actions.write_text('{"runs": []}')
            report = self.ready()
            report.update(source_ref="a" * 40, formula="公式")
            row = report["items"]["hangqing"]
            row.update(delay=stats.quantiles([100, 200]), grace=stats.allowance(stats.quantiles([100, 200])),
                       rotation_grace=None, freshness=None)
            with patch.object(stats, "analyze", return_value=report), patch.object(stats, "render", return_value="隔离统计"), \
                 patch("sys.argv", ["stats", "--root", folder, "--actions", str(actions), "--apply-manifest"]):
                stats.main()
            actual = json.loads(path.read_text())["items"]
            self.assertEqual(actual[0]["grace_minutes"], 300)
            self.assertEqual(actual[1], policy)
            self.assertEqual(actual[2], manifest["items"][2])

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
