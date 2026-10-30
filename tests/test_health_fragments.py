"""生产CLI加载独立登记片段；旧历史回放API不引入未校准的新项目。"""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import health_check as h


class HealthFragmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'system/运行清单.d').mkdir(parents=True)
        self.base={'schema_version':1,'calendar':{'index_csv':'missing','holidays_json':'missing'},'items':[{'id':'base','name':'原项','kind':'self'}]}
        # Calendar的真实格式；无文件时self项仍能运行，日历错误不被伪装成交易日。
        self.base['calendar']={'history':'missing','closures':'missing'}
        self.write('system/运行清单.json',self.base);self.fragment={'schema_version':1,'items':[{'id':'flow-test','name':'新项','kind':'self'}]}
        self.write('system/运行清单.d/flow.json',self.fragment)
    def write(self,path,obj):(self.root/path).write_text(json.dumps(obj,ensure_ascii=False))
    def test_cli_uses_merged_items_and_does_not_modify_base(self):
        before=(self.root/'system/运行清单.json').read_bytes()
        code=h.main(['--root',str(self.root),'--now','2026-10-30T08:20:00+08:00','--json-out','out.json','--report','out.md'])
        self.assertEqual(code,0);self.assertEqual([r['id'] for r in json.loads((self.root/'out.json').read_text())['items']],['base','flow-test'])
        self.assertEqual(before,(self.root/'system/运行清单.json').read_bytes())
    def test_legacy_replay_api_preserves_explicit_main_manifest_scope(self):
        report=h.check(self.root,dt.datetime(2026,10,30,8,tzinfo=h.BJ));self.assertEqual([r['id'] for r in report['items']],['base'])
        report=h.check(self.root,dt.datetime(2026,10,30,8,tzinfo=h.BJ),manifest=h.load_manifest(self.root));self.assertEqual(len(report['items']),2)
    def test_duplicate_or_fragment_calendar_override_rejected(self):
        self.fragment['items'][0]['id']='base';self.write('system/运行清单.d/flow.json',self.fragment)
        with self.assertRaises(ValueError):h.load_manifest(self.root)
        self.fragment['items'][0]['id']='other';self.fragment['calendar']={};self.write('system/运行清单.d/flow.json',self.fragment)
        with self.assertRaises(ValueError):h.load_manifest(self.root)


if __name__=='__main__':unittest.main()
