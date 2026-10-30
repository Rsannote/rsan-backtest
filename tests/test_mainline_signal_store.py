"""年度修订：基表保字节、同键末条、真正变化才追加、大小与幂等。"""
import csv
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import mainline_signal_store as store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.columns=['date','code','value','使用时点说明']
        self.fields=self.columns+['生成时间','修订原因']
        self.base,self.directory=store.paths(self.root,28)
        self.base.parent.mkdir(parents=True)
        self.write(self.base,[self.row('2014-03-03',1),self.row('2014-03-03',2),self.row('2015-03-03',3)])
        self.original=self.base.read_bytes()

    def row(self,day,value,note='旧注记'):
        return {'date':day,'code':'801010','value':value,'使用时点说明':note,'生成时间':'旧时间','修订原因':'旧原因'}

    def write(self,path,rows):
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,self.fields);writer.writeheader();writer.writerows(rows)

    def frame(self,rows):
        return pd.DataFrame([{k:r[k] for k in self.columns} for r in rows])

    def test_base_and_shards_both_take_last_and_missing_revision_is_final(self):
        self.write(self.directory/'2014.csv',[self.row('2014-03-03',4),self.row('2014-03-03','')])
        result=store.read_signal_table(self.root,28)
        self.assertEqual(len(result),2)
        self.assertTrue(pd.isna(result[result.date=='2014-03-03'].iloc[0]['value']))
        self.assertEqual(len(store.read_signal_table(self.root,28,'2014-12-31')),1)
        self.assertEqual(self.base.read_bytes(),self.original)

    def test_only_changed_rows_append_into_correct_year_and_idempotent(self):
        table=self.frame([self.row('2014-03-03',2,'纯注记'),self.row('2015-03-03',4,'新引用')])
        fixed,updates,report=store.plan_revisions(self.root,28,table,'2026-10-30T12:00:00+08:00')
        self.assertEqual(report['追加行'],1);self.assertEqual(fixed.iloc[0]['使用时点说明'],'旧注记')
        self.assertEqual(list(updates),[self.directory/'2015.csv'])
        store.write_revisions(self.root,updates,{'28':report})
        before=(self.directory/'2015.csv').read_bytes()
        _,updates2,again=store.plan_revisions(self.root,28,table,'2026-10-30T13:00:00+08:00')
        self.assertEqual(again['追加行'],0);self.assertFalse(updates2)
        self.assertEqual((self.directory/'2015.csv').read_bytes(),before)
        self.assertEqual(self.base.read_bytes(),self.original)
        self.assertEqual(store.read_signal_table(self.root,28).iloc[-1]['value'],4)

    def test_necessary_visibility_change_appends_and_31_stays_separate(self):
        table=self.frame([self.row('2014-03-03',2,'T13b-v3;D=2013-10-31;V=2013-11-01T00:00:00+08:00')])
        _,updates,report=store.plan_revisions(self.root,28,table,'2026-10-30T12:00:00+08:00')
        self.assertEqual(report['追加行'],1)
        store.write_revisions(self.root,updates,{'28':report})
        self.assertTrue(store.read_signal_table(self.root,31).empty)
        base31,shards31=store.paths(self.root,31)
        self.assertNotEqual(base31,self.base);self.assertEqual(shards31.name,'信号修订')

    def test_header_year_and_size_mismatch_reject_without_base_mutation(self):
        self.write(self.directory/'2014.csv',[self.row('2015-03-03',4)])
        with self.assertRaisesRegex(ValueError,'年度'):store.read_signal_table(self.root)
        (self.directory/'2014.csv').unlink()
        table=self.frame([self.row('2014-03-03',5)])
        with patch('mainline_signal_store.LIMIT',1):
            _,changes,report=store.plan_revisions(self.root,28,table,'2026-10-30T12:00:00+08:00')
            with self.assertRaisesRegex(ValueError,'100MiB'):store.write_revisions(self.root,changes,{'28':report})
        self.assertEqual(self.base.read_bytes(),self.original)
        self.assertFalse((self.directory/'2014.csv').exists())


if __name__=='__main__':unittest.main()
