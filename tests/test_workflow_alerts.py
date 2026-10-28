"""不依赖额外 YAML 库，核对全部工作流的告警接线和定时清单覆盖。"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowAlertTests(unittest.TestCase):
    def test_all_production_workflows_have_failure_alert_and_permissions(self):
        paths = [p for p in (ROOT / ".github/workflows").glob("*.yml") if not p.name.startswith("test-")]
        self.assertEqual(len(paths), 11)
        for path in paths:
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertIn("if: failure()", source)
                self.assertIn("--workflow-failure", source)
                self.assertIn("issues: write", source)
                self.assertIn("group: repo-writer", source)
                self.assertIn("cancel-in-progress: false", source)
                self.assertIn("HEALTH_WORKFLOW_FILE: .github/workflows/" + path.name, source)
                self.assertIn("application/vnd.github.raw+json", source)
                self.assertIn("RUNNER_TEMP", source)

    def test_every_cron_workflow_is_in_manifest(self):
        manifest = json.loads((ROOT / "system/运行清单.json").read_text(encoding="utf-8"))
        registered = {i.get("workflow") for i in manifest["items"]}
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if re.search(r"^  schedule:", path.read_text(encoding="utf-8"), re.M):
                self.assertIn(str(path.relative_to(ROOT)), registered)

    def test_health_schedule_and_recovery_access(self):
        source = (ROOT / ".github/workflows/health-check.yml").read_text(encoding="utf-8")
        self.assertIn('"20 0 * * *"', source)
        self.assertIn('"30 14 * * *"', source)
        self.assertIn("actions: read", source)
        self.assertIn("git pull --rebase origin main", source)
        self.assertIn("logs/健康检查_最新.json", source)

    def test_failure_shell_runs_with_and_without_checkout(self):
        paths = [p for p in (ROOT / ".github/workflows").glob("*.yml") if not p.name.startswith("test-")]
        for path in paths:
            source = path.read_text(encoding="utf-8").split("      - name: 工作流失败告警", 1)[1]
            source = source.split("        run: |\n", 1)[1]
            script = "\n".join(line[10:] for line in source.splitlines() if line.startswith("          "))
            for checked_out in (False, True):
                with self.subTest(workflow=path.name, checkout=checked_out), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "bin").mkdir()
                    (root / "runner").mkdir()
                    (root / "workspace/scripts").mkdir(parents=True)
                    if checked_out:
                        (root / "workspace/scripts/health_alert.py").write_text("已检出的脚本", encoding="utf-8")
                    curl = root / "bin/curl"
                    curl.write_text('#!/bin/sh\n[ "$CHECKED_OUT" = 0 ] || exit 90\nout=""\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = -o ]; then shift; out="$1"; fi\n  shift\ndone\n[ -n "$out" ] || exit 91\nprintf "模拟脚本" > "$out"\n', encoding="utf-8")
                    python = root / "bin/python3"
                    python.write_text('#!/bin/sh\n[ -f "$1" ] && [ "$2" = --workflow-failure ] || exit 92\nprintf "%s" "$1" > "$RESULT"\n', encoding="utf-8")
                    curl.chmod(0o755)
                    python.chmod(0o755)
                    env = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                               GITHUB_WORKSPACE=str(root / "workspace"), RUNNER_TEMP=str(root / "runner"),
                               GITHUB_TOKEN="模拟令牌", GITHUB_REPOSITORY="example/repo", GITHUB_SHA="a" * 40,
                               RESULT=str(root / "result"), CHECKED_OUT=str(int(checked_out)))
                    done = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    expected = root / ("workspace/scripts/health_alert.py" if checked_out else "runner/health_alert.py")
                    self.assertEqual((root / "result").read_text(), str(expected))


if __name__ == "__main__":
    unittest.main()
