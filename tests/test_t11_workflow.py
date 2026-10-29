"""执行真实范围选择代码，push载荷缺文件清单时仍能选择全库。"""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class T11ScopeTests(unittest.TestCase):
    def test_scope_from_actual_unicode_commit_not_webhook_file_attributes(self):
        self.assert_scope('push', 'request', 'full')

    def test_code_only_push_does_not_reuse_old_full_request(self):
        self.assert_scope('push', 'code', 'smoke')

    def test_stale_request_is_rejected(self):
        self.assert_scope('push', 'stale', None)

    def test_manual_statistics_scope_is_respected(self):
        self.assert_scope('workflow_dispatch', 'code', 'stats', input_mode='stats')

    def assert_scope(self, event_name, change, expected, input_mode=''):
        text = (ROOT / '.github/workflows/test-t11-retry.yml').read_text()
        block = text.split('      - name: 选择验收范围', 1)[1].split('      - name: 安装既有依赖', 1)[0]
        code = '\n'.join(line[10:] for line in block.splitlines() if line.startswith('          '))
        script = code.split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(['git', '-C', folder, *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', '测试')
            git('config', 'user.email', 'test@example.invalid')
            request = root / 'logs/test/T11/每日验收请求.json'
            request.parent.mkdir(parents=True)
            request.write_text('{"mode":"full","date":"2000-01-01"}')
            git('add', '.'); git('commit', '-qm', '基线')
            if change in ('request', 'stale'):
                today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
                request.write_text(json.dumps({'mode':'full','date':str(today if change == 'request' else today-dt.timedelta(days=1))}))
            else:
                (root / 'code.txt').write_text('代码变更')
            git('add', '.'); git('commit', '-qm', '测试触发')
            event = root / 'event.json'; event.write_text('{"commits":[{"id":"no-file-attributes"}]}')
            output = root / 'output.txt'
            env = dict(os.environ, INPUT_MODE=input_mode, GITHUB_EVENT_PATH=str(event),
                       GITHUB_EVENT_NAME=event_name, GITHUB_SHA=git('rev-parse', 'HEAD'), GITHUB_OUTPUT=str(output))
            completed = subprocess.run(['python3', '-c', script], cwd=root, env=env, text=True, capture_output=True)
            if expected is None:
                self.assertNotEqual(completed.returncode, 0)
            else:
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(output.read_text(), 'mode='+expected+'\n')


if __name__ == '__main__':
    unittest.main()
