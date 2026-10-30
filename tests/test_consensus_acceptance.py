"""用临时合成档案验证日历连续性、旧实现/小样本/坏SHA排除。"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import check_consensus_acceptance as check
import update_consensus as u

ROOT=Path(__file__).resolve().parents[1]


class ConsensusAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for rel in ('scripts/consensus_common.py','scripts/update_consensus.py','scripts/probe_consensus.py','data/行情库/指数/sh000300.csv','system/交易所休市.json'):
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/rel,p)
        self.now=dt.datetime(2026,10,30,12,tzinfo=u.BJ)
    def record(self,date,eligible=True):
        row={f:'' for f in u.FIELDS};row.update({'code':'合成测试','来源':'同花顺','抓取时间':date+'T10:00:00+08:00','预测本年':2026,'预测次年':2027})
        text=u.csv_text(u.FIELDS,[row]);base=self.root/'data/一致预期';p=base/date[:7]/(date+'.csv');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        proof={'日期':date,'完整范围':1,'主来源':'同花顺','实现指纹':u.fingerprint(self.root),'规则SHA256':check.RULE_SHA,'计入五交易日验收':eligible,'快照SHA256':hashlib.sha256(text.encode()).hexdigest()}
        p=base/'验收'/(date+'.json');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(proof));return p
    def test_weekend_skipped_and_missing_trading_day_breaks(self):
        dates=('2026-10-23','2026-10-26','2026-10-27','2026-10-28','2026-10-29')
        for d in dates:self.record(d)
        self.assertEqual(check.check(self.root,self.now)['最近连续合格交易日数'],5)
        (self.root/'data/一致预期/验收/2026-10-27.json').unlink()
        self.assertEqual(check.check(self.root,self.now)['最近连续合格交易日数'],2)
    def test_small_sample_old_implementation_and_bad_sha_excluded(self):
        self.record('2026-10-27',False);p=self.record('2026-10-28');proof=json.loads(p.read_text());proof['实现指纹']='旧实现';p.write_text(json.dumps(proof))
        self.record('2026-10-29');p=self.root/'data/一致预期/2026-10/2026-10-29.csv';p.write_text(p.read_text()+'\n')
        report=check.check(self.root,self.now);self.assertEqual(report['最近连续合格交易日数'],0);self.assertEqual(len(report['排除']),3)


if __name__=='__main__':unittest.main()
