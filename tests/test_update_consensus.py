"""离线快照事务边界、四周留空、跨年同一预测年度与来源保护。"""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import update_consensus as u
import consensus_common as c

ROOT=Path(__file__).resolve().parents[1]


class ConsensusSnapshotTests(unittest.TestCase):
    def member(self):return {x:{'code':x,'申万一级代码':'1','申万一级':'合成行业'} for x in ('a','b','c')}
    def row(self,code='a',current=2,nextyear=3,year=2026,source='同花顺',industry='1'):
        return {'code':code,'申万一级代码':industry,'申万一级':'合成行业','来源':source,'预测本年':year,'预测次年':year+1,'eps_本年':current,'eps_次年':nextyear}
    def test_four_weeks_missing_not_zero(self):
        r=u.industry_changes('2026-10-30',[self.row()],{'2026-10-05':[self.row(current=1)]},self.member())
        self.assertTrue(all(x['上修家数']=='' and x['上修占比']=='' and x['基准日期']=='' for x in r))
    def test_same_fiscal_year_across_new_year(self):
        old=self.row(year=2025,current=99,nextyear=1);new=self.row(year=2026,current=2,nextyear=4)
        rows=u.industry_changes('2026-01-30',[new],{'2025-12-31':[old]},self.member())
        self.assertEqual(rows[0]['上修家数'],1);self.assertEqual(rows[0]['上修占比'],1)
        self.assertEqual(rows[1]['可比家数'],0);self.assertEqual(rows[1]['上修占比'],'')
    def test_changed_source_or_industry_not_comparable(self):
        for old in (self.row(source='东财批量'),self.row(industry='2')):
            rows=u.industry_changes('2026-10-30',[self.row()],{'2026-10-02':[old]},self.member());self.assertEqual(rows[0]['可比家数'],0)
    def test_denominator_only_paired_values_and_down_unchanged(self):
        current=[self.row('a',3),self.row('b',1),self.row('c',None)]
        past=[self.row('a',2),self.row('b',2),self.row('c',2)]
        rows=u.industry_changes('2026-10-30',current,{'2026-10-02':past},self.member())
        self.assertEqual((rows[0]['上修家数'],rows[0]['下修家数'],rows[0]['可比家数']),(1,1,2));self.assertEqual(rows[0]['上修占比'],.5)
    def test_partial_eps_keeps_available_year_without_fake_zero(self):
        html='预测年报每股收益<table><tr><th>年度</th><th>预测机构数</th><th>均值</th></tr><tr><td>2026</td><td>4</td><td>0</td></tr></table>'
        r=c.parse_ths_partial(html,2026);self.assertEqual(r['eps'],{'2026':0});self.assertNotIn('2027',r['eps'])
        with self.assertRaises(ValueError):c.parse_ths_partial(html.replace('每股收益','净利润'),2026)
    def test_decision_cannot_be_manually_overridden(self):
        report=json.loads((ROOT/'logs/test/T12b/来源比对_50.json').read_text())
        self.assertEqual(u.checked_decision(report)['主来源'],'同花顺')
        report['来源决策']['主来源']='东财批量'
        with self.assertRaises(ValueError):u.checked_decision(report)
    def test_sampling_cannot_be_changed_after_results(self):
        report=json.loads((ROOT/'logs/test/T12b/来源比对_50.json').read_text())
        report['固定抽样代码']=list(reversed(report['固定抽样代码']))
        with self.assertRaises(ValueError):u.checked_decision(report)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for rel in ('scripts/consensus_common.py','scripts/update_consensus.py','scripts/probe_consensus.py','scripts/v3_select.py','data/v3/申万成分.json','data/行情库/指数/sh000300.csv','system/交易所休市.json'):
            out=self.root/rel;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/rel,out)
    def context(self):
        report=json.loads((ROOT/'logs/test/T12b/来源比对_50.json').read_text());members,sha=c.industry_members(self.root);stock=members[sorted(members)[0]]
        obj={'日期':'2026-10-30','year':2026,'来源比对':report,'主来源':'同花顺','warnings':[],'批次数':1,'总数':1,'stocks':[stock],
             '实现指纹':u.fingerprint(self.root),'成分SHA256':sha,'东财计数':{},'东财计数抓取时间':None}
        return obj
    def test_incomplete_batch_preserves_all_formal_data(self):
        context=self.context();base=self.root/'data/一致预期';base.mkdir(parents=True);old=base/'已有.txt';old.write_bytes(b'original')
        r=u.assemble(self.root,context,self.root/'missing',dt.datetime(2026,10,30,11,tzinfo=u.BJ))
        self.assertEqual(r['status'],'失败');self.assertEqual(list(base.rglob('*')),[old]);self.assertEqual(old.read_bytes(),b'original')
    def test_complete_snapshot_append_replay_and_changed_input_rejected(self):
        context=self.context();stock=context['stocks'][0]
        ths={'eps':{'2026':1,'2027':2},'机构数':{'2026':4,'2027':3},'抓取时间':'2026-10-30T10:00:00+08:00'}
        row=u.snapshot_row(stock,context,ths);batches=self.root/'parts';batches.mkdir();part={'批次':0,'日期':context['日期'],'主来源':'同花顺','实现指纹':context['实现指纹'],'errors':[],'warnings':[],'rows':[row],'完成':True}
        u.dump(batches/'批次_0.json',part);now=dt.datetime(2026,10,30,11,tzinfo=u.BJ)
        self.assertEqual(u.assemble(self.root,context,batches,now)['status'],'正常')
        base=self.root/'data/一致预期';before={p.relative_to(base):p.read_bytes() for p in base.rglob('*') if p.is_file() and p.name not in ('最新.md','最新.json')}
        self.assertEqual(u.assemble(self.root,context,batches,now)['status'],'正常')
        self.assertTrue(all((base/p).read_bytes()==raw for p,raw in before.items()))
        self.assertFalse(u.prepare(self.root,self.root/'work',now=now)['执行'])
        part['rows'][0]['eps_本年']=9;u.dump(batches/'批次_0.json',part)
        with self.assertRaises(ValueError):u.assemble(self.root,context,batches,now)
        self.assertTrue(all((base/p).read_bytes()==raw for p,raw in before.items()))
    def test_weekend_does_not_fetch(self):
        with patch.object(u,'fetch_em',side_effect=AssertionError('不应取数')):
            r=u.prepare(self.root,self.root/'work',now=dt.datetime(2026,10,31,11,tzinfo=u.BJ));self.assertFalse(r['执行'])


if __name__=='__main__':unittest.main()
