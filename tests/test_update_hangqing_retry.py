"""T11离线：失败重试调度、隔离晋升及硬截止；不请求真实行情。"""
import ast
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hangqing_retry", ROOT / "scripts/update_hangqing.py")
hq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hq)
sys.path.insert(0, str(ROOT / "scripts"))
VERIFY_SPEC = importlib.util.spec_from_file_location("verify_t11", ROOT / "scripts/verify_t11_retry.py")
verify = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify)


def item(code="600900", typ="股票"):
    return {"code": code, "name": "测试" + code, "type": typ}


def failed(value, full=False, status="失败"):
    return dict(value, status=status, full=full, notes=["HTTPError 501：主循环失败"])


class Clock:
    def __init__(self):
        self.now, self.waits = 0., []
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class RetryTests(unittest.TestCase):
    def test_serial_original_order_second_round_recovery_and_mode_preserved(self):
        items = [item(str(n)) for n in range(4)]
        reps = [failed(items[0], True), failed(items[1]), dict(items[2], status="ok", full=True, notes=[]), failed(items[3], status="未轮到")]
        calls, clock = [], Clock()
        def attempt(value, full, timeout, pace):
            self.assertIs(threading.current_thread(), threading.main_thread())
            calls.append((value["code"], full, timeout, pace))
            if value["code"] == "0" and len(calls) == 1:
                return {"status": "失败", "notes": ["HTTP 501"]}
            return dict(value, status="ok", notes=[])
        result = hq.retry_failed(items, reps, attempt=attempt, clock=clock, sleep=clock.sleep)
        self.assertEqual([c[0] for c in calls], ["0", "1", "0"])
        self.assertEqual([c[1] for c in calls], [True, False, True])
        self.assertEqual(clock.waits, [60, 2, 60])
        self.assertEqual(reps[0]["retry_round"], 2)
        self.assertTrue(reps[0]["full"])
        self.assertEqual(reps[3]["status"], "未轮到")
        self.assertEqual(result["initial_failed"], 2)
        self.assertEqual(len(result["recovered"]), 2)

    def test_permanent_non_rate_limit_error_retains_original_and_two_failures(self):
        value, clock = item(), Clock()
        reps = [failed(value)]
        def attempt(*args):
            return {"status": "失败", "notes": ["ValueError：源格式不合法（非限流）"]}
        result = hq.retry_failed([value], reps, attempt=attempt, clock=clock, sleep=clock.sleep)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(reps[0]["status"], "失败")
        self.assertIn("HTTPError 501", reps[0]["notes"][0])
        self.assertEqual(sum("非限流" in note for note in reps[0]["notes"]), 2)

    def test_wait_is_inside_total_budget_no_attempt_after_exhaustion(self):
        value, clock = item(), Clock()
        reps = [failed(value)]
        with patch.object(hq, "retry_attempt") as attempt:
            result = hq.retry_failed([value], reps, budget=50, clock=clock, sleep=clock.sleep)
        attempt.assert_not_called()
        self.assertEqual(result["elapsed_seconds"], 50)
        self.assertTrue(result["budget_exhausted"])

    def test_remaining_budget_caps_long_single_symbol(self):
        value, clock = item(), Clock()
        reps, timeouts = [failed(value)], []
        def attempt(value, full, timeout, pace):
            timeouts.append(timeout)
            clock.sleep(timeout)
            raise TimeoutError("故障注入：慢请求已终止")
        result = hq.retry_failed([value], reps, budget=100, item_timeout=120, attempt=attempt, clock=clock, sleep=clock.sleep)
        self.assertEqual(timeouts, [40])
        self.assertEqual(result["elapsed_seconds"], 100)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(reps[0]["status"], "失败")

    def test_no_failures_and_disabled_retry_do_not_wait(self):
        value, clock = item(), Clock()
        with patch.object(hq, "retry_attempt") as attempt:
            result = hq.retry_failed([value], [dict(value, status="ok")], clock=clock, sleep=clock.sleep)
            hq.retry_failed([value], [failed(value)], rounds=0, clock=clock, sleep=clock.sleep)
        attempt.assert_not_called()
        self.assertEqual(clock.waits, [])
        self.assertEqual(result["initial_failed"], 0)

    def test_cli_rejects_invalid_parameters_before_opening_data(self):
        cases = [("--retry-rounds", "3"), ("--retry-rounds", "-1"), ("--retry-wait", "-1"),
                 ("--retry-budget", "601"), ("--retry-budget", "nan"), ("--retry-pace", "0.8"),
                 ("--retry-item-timeout", "0"), ("--workers", "0"), ("--budget", "inf")]
        for args in cases:
            with self.subTest(args=args), patch.object(hq, "LIST", "/definitely/not/opened.csv"), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    hq.main(list(args))
                self.assertEqual(error.exception.code, 2)

    def test_t1_and_update_one_critical_ast_remain_identical_to_merged_main(self):
        baseline = subprocess.check_output(["git", "-C", str(ROOT), "show", "4e061f3635af8e3cc21cd359b33fb054a698c693:scripts/update_hangqing.py"], text=True)
        old, new = ast.parse(baseline), ast.parse((ROOT / "scripts/update_hangqing.py").read_text())
        for name in ("update_one", "factors", "write_store", "drop_spikes", "fetch_full", "fetch_raw", "fetch_segment", "get"):
            find = lambda tree: next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            self.assertEqual(ast.dump(find(old)), ast.dump(find(new)), name)

    def test_real_acceptance_excludes_morning_old_date_injection_and_smoke(self):
        calendar = SimpleNamespace(is_open=lambda date: True)
        started = verify.hc.timestamp("2026-10-29T16:00:00+08:00")
        proof = {"退出码": 0, "最终失败与原因": [], "关键标的日期": {"sh000300": "2026-10-29"}, "实际增加耗时秒": 10}
        args = SimpleNamespace(all=True, inject_501=None)
        self.assertTrue(verify.acceptance_day(proof, args, calendar, started, started)[0])
        self.assertFalse(verify.acceptance_day(proof, args, calendar, started.replace(hour=8), started)[0])
        self.assertFalse(verify.acceptance_day(proof, SimpleNamespace(all=False, inject_501=None), calendar, started, started)[0])
        self.assertFalse(verify.acceptance_day(proof, SimpleNamespace(all=True, inject_501="600872"), calendar, started, started)[0])
        proof["关键标的日期"]["sh000300"] = "2026-10-28"
        self.assertFalse(verify.acceptance_day(proof, args, calendar, started, started)[0])

    def test_three_day_count_deduplicates_and_requires_consecutive_trading_days(self):
        calendar = SimpleNamespace(is_open=lambda date: date.weekday() < 5)
        self.assertEqual(verify.consecutive_days(["2026-10-23", "2026-10-23", "2026-10-26", "2026-10-27"], calendar), 3)
        self.assertEqual(verify.consecutive_days(["2026-10-23", "2026-10-27"], calendar), 1)

    def test_three_days_never_mix_versions_but_document_commits_do_not_reset(self):
        fingerprint = {"sha256": "same-code", "files": {"scripts/update_hangqing.py": "a", "scripts/verify_t11_retry.py": "b"}}
        def record(date, version=fingerprint, commit="commit-a"):
            return {"交易数据日期": date, "计入完整真实交易日验收": True, "验收实现指纹": version, "分支代码": commit}
        records = [record("2026-10-26"), record("2026-10-26", commit="docs-only"),
                   record("2026-10-27", {"sha256": "old-code"}), record("2026-10-28")]
        dates = verify.qualified_dates(records, fingerprint)
        self.assertEqual(dates, ["2026-10-26", "2026-10-28"])
        self.assertEqual(verify.consecutive_days(dates, SimpleNamespace(is_open=lambda date: date.weekday() < 5)), 1)
        self.assertEqual(verify.qualified_dates([{"交易数据日期": "2026-10-27", "计入完整真实交易日验收": True}], fingerprint), [])


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name) / "行情库"
        self.library.mkdir()
        self.value = item()
        self.price = self.library / "股票/600900.csv"
        self.price.parent.mkdir()
        self.events = self.library / "分红表/600900.json"
        self.events.parent.mkdir()
        self.rows = pd.DataFrame([[1., 1.2, .9, 1.1, 100., 1.], [1.1, 1.3, 1., 1.2, 110., 1.]],
                                 index=pd.to_datetime(["2026-10-27", "2026-10-28"]), columns=hq.COLS + ["factor"])
        hq.write_store(self.price, self.rows)
        self.events.write_text("[]", encoding="utf-8")
        self.original = {self.price: self.price.read_bytes(), self.events: self.events.read_bytes()}
        self.context = patch.object(hq, "LIB", str(self.library))
        self.context.start()
        self.addCleanup(self.context.stop)

    def complete(self, command, **kwargs):
        config = json.loads(Path(command[-2]).read_text())
        self.assertEqual(config["full"], True)
        self.assertEqual(config["pace"], 2)
        library = Path(config["library"])
        output = library / "股票/600900.csv"
        self.assertEqual(output.read_bytes(), self.original[self.price])
        rows = pd.concat([self.rows, pd.DataFrame([[1.2, 1.4, 1.1, 1.3, 120., 1.]], index=pd.to_datetime(["2026-10-29"]), columns=hq.COLS + ["factor"])])
        hq.write_store(output, rows)
        rep = dict(self.value, status="ok", notes=[], first="2026-10-27", last="2026-10-29", rows=3,
                   events=0, last_event="", events_sha256=hq.sha(library / "分红表/600900.json"), factor_last=1., sha256=hq.sha(output)[:16], added=1)
        Path(command[-1]).write_text(json.dumps(rep), encoding="utf-8")
        self.staged = library
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_validated_success_promotes_exact_bytes_and_preserves_old_prefix(self):
        with patch.object(hq.subprocess, "run", side_effect=self.complete):
            rep = hq.retry_attempt(self.value, True, 5, 2)
        self.assertEqual(rep["last"], "2026-10-29")
        self.assertEqual(self.price.read_bytes().splitlines()[:3], self.original[self.price].splitlines())
        self.assertEqual(self.events.read_bytes(), self.original[self.events])
        self.assertFalse(self.staged.exists())

    def test_bad_sha_or_missing_events_is_never_promoted(self):
        for bad in ("sha", "events"):
            def complete(command, **kwargs):
                self.complete(command, **kwargs)
                if bad == "sha":
                    target = Path(command[-1]); rep = json.loads(target.read_text()); rep["sha256"] = "bad"; target.write_text(json.dumps(rep))
                else:
                    (self.staged / "分红表/600900.json").unlink()
                return subprocess.CompletedProcess(command, 0, "", "")
            with self.subTest(bad=bad), patch.object(hq.subprocess, "run", side_effect=complete):
                with self.assertRaises((ValueError, OSError)):
                    hq.retry_attempt(self.value, True, 5, 2)
            for path, original in self.original.items():
                self.assertEqual(path.read_bytes(), original)

    def test_actual_blocking_child_is_killed_without_corrupting_original(self):
        real_run = subprocess.run
        def block(command, **kwargs):
            config = json.loads(Path(command[-2]).read_text())
            target = str(Path(config["library"]) / "股票/600900.csv")
            code = "from pathlib import Path; import time; Path(" + repr(target) + ").write_text('未完成的坏CSV'); time.sleep(30)"
            return real_run([sys.executable, "-c", code], **kwargs)
        started = time.monotonic()
        with patch.object(hq.subprocess, "run", side_effect=block):
            with self.assertRaises(TimeoutError):
                hq.retry_attempt(self.value, False, .25, 2)
        self.assertLess(time.monotonic() - started, 3)
        for path, original in self.original.items():
            self.assertEqual(path.read_bytes(), original)

    def test_main_retry_success_updates_state_and_logs_separate_recovery(self):
        listing = Path(self.temp.name) / "标的清单.csv"
        listing.write_text("code,name,type\n600900,测试600900,股票\n", encoding="utf-8")
        root = Path(self.temp.name)
        def recover(items, reps, **kwargs):
            rep = dict(items[0], status="ok", notes=[], first="2026-10-27", last="2026-10-29", rows=3,
                       events=0, last_event="", factor_last=1., sha256="validated", added=1, full=True, retry_round=2)
            reps[0] = rep
            return {"initial_failed": 1, "recovered": [dict(items[0], round=2, full=True)], "rounds": 2,
                    "attempts": 2, "elapsed_seconds": 130., "budget_seconds": 600}
        with patch.object(hq, "LIST", str(listing)), patch.object(hq, "ROOT", str(root)), \
             patch.object(hq, "update_one", return_value=failed(self.value, True)), patch.object(hq, "retry_failed", side_effect=recover), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(hq.main(["--full", "--workers", "1"]), 0)
        with (self.library / "状态.csv").open() as file:
            import csv
            state = next(csv.DictReader(file))
        self.assertEqual(state["last"], "2026-10-29")
        self.assertEqual(state["rows"], "3")
        report = (root / "logs/行情库_最新.md").read_text()
        self.assertIn("主循环失败：1", report)
        self.assertIn("成功 1，失败 0", report)
        self.assertIn("## 补抓成功", report)
        self.assertNotIn("## 失败", report)

    def test_atomic_promotion_error_rolls_back_already_replaced_price(self):
        real_replace, calls = hq.os.replace, []
        def replace(source, target):
            calls.append(str(target))
            if len(calls) == 2:
                raise OSError("故障注入：第二文件晋升失败")
            return real_replace(source, target)
        with patch.object(hq.subprocess, "run", side_effect=self.complete), patch.object(hq.os, "replace", side_effect=replace):
            with self.assertRaises(OSError):
                hq.retry_attempt(self.value, True, 5, 2)
        for path, original in self.original.items():
            self.assertEqual(path.read_bytes(), original)

    def test_main_rotation_keeps_original_full_flag_for_each_retry(self):
        week = hq.date.today().isocalendar()[1] % 4
        codes = [f"600{i:03d}" for i in range(100)]
        bucket = lambda code: int(hq.hashlib.md5(code.encode()).hexdigest(), 16) % 4
        selected = [next(c for c in codes if bucket(c) == week), next(c for c in codes if bucket(c) != week)]
        listing = Path(self.temp.name) / "标的清单.csv"
        listing.write_text("code,name,type\n" + "".join(f"{c},测试{c},股票\n" for c in selected))
        seen = []
        def update(value, full):
            seen.append((value["code"], full))
            return failed(value, full)
        def retry(items, reps, **kwargs):
            self.assertEqual([(r["code"], r["full"]) for r in reps], [(selected[0], True), (selected[1], False)])
            return {"initial_failed": 2, "recovered": [], "rounds": 0, "attempts": 0, "elapsed_seconds": 0, "budget_seconds": 600}
        with patch.object(hq, "LIST", str(listing)), patch.object(hq, "ROOT", self.temp.name), \
             patch.object(hq, "update_one", side_effect=update), patch.object(hq, "retry_failed", side_effect=retry), \
             patch("sys.stdout", new_callable=io.StringIO):
            hq.main(["--full", "--rotate", "--workers", "1"])
        self.assertEqual(seen, [(selected[0], True), (selected[1], False)])


if __name__ == "__main__":
    unittest.main()
