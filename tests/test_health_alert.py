"""离线验证 Issue 生命周期、同日去重和工作流恢复边界；不访问 GitHub。"""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("health_alert", Path(__file__).resolve().parents[1] / "scripts/health_alert.py")
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)


class FakeGitHub:
    def __init__(self):
        self.rows, self.comments, self.runs = [], [], []

    def ensure_label(self):
        pass

    def issues(self):
        return copy.deepcopy(self.rows)

    def request(self, method, path, payload=None):
        if path.startswith("actions/workflows/"):
            return {"workflow_runs": self.runs}
        if method == "POST" and path == "issues":
            n = len(self.rows) + 1
            row = {"number": n, "html_url": f"https://github.com/example/repo/issues/{n}", "state": "open", **payload}
            self.rows.append(row)
            return copy.deepcopy(row)
        number = int(path.split("/")[1])
        if path.endswith("/comments"):
            self.comments.append((number, payload["body"]))
            return {}
        row = self.rows[number - 1]
        row.update(payload)
        if payload.get("state") == "closed":
            row["closed_at"] = "2026-10-28T13:00:00Z"
        return copy.deepcopy(row)


def report(status, checked="2026-10-28T20:00:00+08:00", workflow=None, item="hangqing"):
    row = {"id": item, "name": "行情库", "status": status, "reasons": ["模拟故障" if status != "正常" else "已恢复"]}
    if workflow:
        row["workflow"] = workflow
    return {"checked_at": checked, "items": [row]}


class AlertTests(unittest.TestCase):
    def test_same_day_repeat_comments_without_duplicate_issue(self):
        api = FakeGitHub()
        first = alerts.sync_report(api, report("部分失败"))
        second = alerts.sync_report(api, report("部分失败"))
        self.assertEqual(len(api.rows), 1)
        self.assertEqual(first[0]["number"], second[0]["number"])
        self.assertEqual(second[0]["action"], "追加评论")
        self.assertEqual(len(api.comments), 1)

    def test_recover_closes_then_same_day_relapse_reopens_same_issue(self):
        api = FakeGitHub()
        alerts.sync_report(api, report("延迟"))
        restored = alerts.sync_report(api, report("正常"))
        self.assertEqual(restored[0]["state"], "closed")
        reopened = alerts.sync_report(api, report("失败"))
        self.assertEqual(len(api.rows), 1)
        self.assertEqual(reopened[0]["action"], "重新打开")

    def test_new_day_has_one_issue_and_recovery_closes_previous_days(self):
        api = FakeGitHub()
        alerts.sync_report(api, report("失败"))
        alerts.sync_report(api, report("失败", "2026-10-29T08:20:00+08:00"))
        self.assertEqual(len(api.rows), 2)
        alerts.sync_report(api, report("正常", "2026-10-29T22:30:00+08:00"))
        self.assertTrue(all(i["state"] == "closed" for i in api.rows))

    def test_namespaces_do_not_close_real_alerts_during_drill(self):
        api = FakeGitHub()
        alerts.sync_report(api, report("失败"))
        alerts.sync_report(api, report("失败"), "test")
        alerts.sync_report(api, report("正常"), "test")
        self.assertEqual([i["state"] for i in api.rows], ["open", "closed"])

    def test_beijing_day_used_even_when_input_is_utc(self):
        api = FakeGitHub()
        alerts.sync_report(api, report("失败", "2026-10-28T17:00:00Z"))
        self.assertEqual(alerts.metadata(api.rows[0])["date"], "2026-10-29")

    def test_workflow_failure_needs_new_success_not_just_old_output(self):
        api = FakeGitHub()
        wf = {"file": ".github/workflows/update-hangqing.yml", "run_id": 10, "attempt": 1}
        alerts.sync_report(api, report("失败", workflow=wf), workflow_event=True)
        api.runs = [{"id": 9, "status": "completed", "conclusion": "success", "updated_at": "2026-10-28T19:00:00+08:00"}]
        alerts.sync_report(api, report("正常"))
        self.assertEqual(api.rows[0]["state"], "open")
        api.runs = [{"id": 11, "status": "completed", "conclusion": "failure", "updated_at": "2026-10-28T20:10:00+08:00"}]
        alerts.sync_report(api, report("正常"))
        self.assertEqual(api.rows[0]["state"], "open")
        api.runs = [{"id": 12, "status": "completed", "conclusion": "success", "updated_at": "2026-10-28T20:10:00+08:00"}]
        alerts.sync_report(api, report("部分失败", "2026-10-28T20:30:00+08:00"))
        self.assertEqual(api.rows[0]["state"], "open")
        alerts.sync_report(api, report("正常", "2026-10-28T20:30:00+08:00"))
        self.assertEqual(api.rows[0]["state"], "closed")

    def test_successful_rerun_same_id_higher_attempt_recovers(self):
        api = FakeGitHub()
        wf = {"file": ".github/workflows/check-sources.yml", "run_id": 10, "attempt": 1}
        alerts.sync_report(api, report("失败", workflow=wf, item="workflow:check-sources.yml"), workflow_event=True)
        api.runs = [{"id": 10, "run_attempt": 2, "status": "completed", "conclusion": "success", "updated_at": "2026-10-28T20:10:00+08:00"}]
        alerts.sync_report(api, {"checked_at": "2026-10-28T20:30:00+08:00", "items": []})
        self.assertEqual(api.rows[0]["state"], "closed")

    def test_running_success_not_a_recovery(self):
        api = FakeGitHub()
        wf = {"file": ".github/workflows/check-sources.yml", "run_id": 10, "attempt": 1}
        alerts.sync_report(api, report("失败", workflow=wf), workflow_event=True)
        api.runs = [{"id": 11, "status": "in_progress", "conclusion": None}]
        alerts.sync_report(api, report("正常"))
        self.assertEqual(api.rows[0]["state"], "open")

    def test_broken_manifest_does_not_close_scheduled_output_alert(self):
        api = FakeGitHub()
        wf = {"file": ".github/workflows/update-hangqing.yml", "run_id": 10, "attempt": 1}
        alerts.sync_report(api, report("失败", workflow=wf), workflow_event=True)
        api.runs = [{"id": 11, "status": "completed", "conclusion": "success", "updated_at": "2026-10-28T20:10:00+08:00"}]
        alerts.sync_report(api, report("失败", "2026-10-28T20:30:00+08:00", item="manifest"))
        self.assertEqual(api.rows[0]["state"], "open")
        self.assertEqual(len(api.rows), 2)

    def test_old_run_rerun_cannot_be_recovered_by_earlier_higher_id_success(self):
        api = FakeGitHub()
        wf = {"file": ".github/workflows/update-hangqing.yml", "run_id": 10, "attempt": 2}
        alerts.sync_report(api, report("失败", "2026-10-28T22:00:00+08:00", workflow=wf), workflow_event=True)
        api.runs = [{"id": 11, "status": "completed", "conclusion": "success", "updated_at": "2026-10-28T21:00:00+08:00"},
                    {"id": 10, "run_attempt": 2, "status": "completed", "conclusion": "failure", "updated_at": "2026-10-28T22:01:00+08:00"}]
        alerts.sync_report(api, report("正常", "2026-10-28T22:30:00+08:00"))
        self.assertEqual(api.rows[0]["state"], "open")
        api.runs.append({"id": 9, "run_attempt": 3, "status": "completed", "conclusion": "success", "updated_at": "2026-10-28T22:15:00+08:00"})
        alerts.sync_report(api, report("正常", "2026-10-28T22:30:00+08:00"))
        self.assertEqual(api.rows[0]["state"], "closed")

    def test_missing_completion_time_does_not_recover(self):
        api = FakeGitHub()
        wf = {"file": ".github/workflows/update-hangqing.yml", "run_id": 10}
        alerts.sync_report(api, report("失败", workflow=wf), workflow_event=True)
        api.runs = [{"id": 11, "status": "completed", "conclusion": "success"}]
        alerts.sync_report(api, report("正常", "2026-10-28T22:30:00+08:00"))
        self.assertEqual(api.rows[0]["state"], "open")

    def test_malformed_foreign_marker_is_ignored(self):
        for value in ('["namespace","item","date"]', '{"namespace":"production","item":3,"date":"2026-10-28"}'):
            self.assertIsNone(alerts.metadata({"body": "<!-- rsan-health-v1 " + value + " -->"}))


if __name__ == "__main__":
    unittest.main()
