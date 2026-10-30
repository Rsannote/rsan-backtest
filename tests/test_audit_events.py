"""真实审计回归：已填人工可保留，新生成判读和改写旧字节须拒绝。"""
import csv
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import audit_events as audit
import test_build_events as fixture


class AuditTests(unittest.TestCase):
    def setUp(self):
        helper=fixture.EventTests();helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.root,self.now=helper.root,helper.now
        days=helper.fixture()
        values=[100+i for i in range(len(days))]
        values[-12:]=[220,210,180,160,170,200,230,210,190,170,160,180]
        helper.write('data/行情库/指数/sh000300.csv',['date','close','factor'],[(d,v,1) for d,v in zip(days,values)])
        schema=self.root/'system/事件库格式.md';schema.parent.mkdir(parents=True)
        schema.write_text('合成验收格式\n')
        audit.events.run(self.root,self.now)
        # 合成序列没有完整距离候选；本组只检验人工字段与旧字节保护。
        report_path=self.root/'logs/事件库_最新.json'
        report=json.loads(report_path.read_text())
        self.assertEqual(report['主表排名'],[])
        report['标准化']['总体标准差']={}
        report_path.write_text(json.dumps(report,ensure_ascii=False))
        self.paths=[self.root/'data/事件库'/n for n in ('大调整.csv','大反弹.csv','大调整_10.csv')]
        for path in self.paths:
            with path.open(newline='') as stream:rows=list(csv.DictReader(stream))
            self.assertTrue(rows)
            for field in audit.events.HUMAN:rows[0][field]='Claude人工原文：'+field
            out=io.StringIO(newline='');writer=csv.DictWriter(out,audit.events.FIELDS,lineterminator='\r\n')
            writer.writeheader();writer.writerows(rows);path.write_bytes(out.getvalue().encode())

    def test_existing_human_text_and_crlf_are_accepted_and_unchanged(self):
        before={path:path.read_bytes() for path in self.paths}
        with patch.object(audit,'ROOT',self.root):audit.main()
        self.assertTrue(all(path.read_bytes()==raw for path,raw in before.items()))

    def test_program_generated_human_conclusion_is_rejected(self):
        original=audit.events.record
        def invented(*args,**kwargs):
            row=original(*args,**kwargs);row['触发事件']='程序擅填';return row
        with patch.object(audit,'ROOT',self.root),patch.object(audit.events,'record',side_effect=invented):
            with self.assertRaisesRegex(AssertionError,'程序生成了人工判读'):audit.main()

    def test_silent_old_text_rewrite_is_rejected(self):
        original=audit.events.append_records
        def corrupt(path,*args,**kwargs):
            count=original(path,*args,**kwargs)
            path=Path(path);path.write_bytes(path.read_bytes().replace('Claude人工原文'.encode(),'被改写'.encode()))
            return count
        with patch.object(audit,'ROOT',self.root),patch.object(audit.events,'append_records',side_effect=corrupt):
            with self.assertRaises(AssertionError):audit.main()


if __name__=='__main__':unittest.main()
