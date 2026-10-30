"""T13合成输入：五维口径、修订、披露时点和缺失；无联网。"""
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import mainline_sources as src
import mainline_signals as sig


class SignalsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        (self.root/'scripts').mkdir();shutil.copyfile(ROOT/'scripts/v3_select.py',self.root/'scripts/v3_select.py')
        self.codes=sorted({f'801{i:03d}' for i in range(28)}|src.NEW);self.ordinary=sorted(set(self.codes)-src.NEW);self.aggregate=self.ordinary[-1]
        self.reg=[{'申万代码':c,'行业':'合成'+c,'久期':'长' if i%2 else '短','风格':'成长','新闻标签':'标签','概念关键词':'半导体;芯片','主线判定':'排除' if c==self.aggregate else '参与'} for i,c in enumerate(self.codes)]
        self.write('system/行业清单.csv',list(self.reg[0]),self.reg)
        self.write('system/催化日历.csv',['日期','事件','相关行业','确定性'],[{'日期':'2026-10-29','事件':'合成事件','相关行业':'全市场','确定性':'确定'},{'日期':'2026-11-下旬','事件':'待定事件','相关行业':'全市场','确定性':'预计'}])
    def write(self,path,cols,rows):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True)
        with p.open('w',newline='') as f:w=csv.DictWriter(f,cols);w.writeheader();w.writerows(rows)
    def json(self,path,obj):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,ensure_ascii=False))
    def source(self):return src.Sources(self.root,src.registry(self.root))
    def flow(self):
        rows=[]
        for day in ['2014-02-21','2021-12-13','2026-10-29']:
            for i,c in enumerate(self.codes):
                if c in src.NEW and day<'2021-12-13':continue
                row={'date':day,'code':c,'成交额原值':1,'偏离60日线':.1,'相对沪深300_20':i/100,'相对沪深300_60':i/100}
                for scope in (28,31):
                    for key in ['占比','总额','占比5日变化','占比20日变化','占比5日均','占比20日均','占比5日均较20日均','占比60日分位','占比一年分位']:
                        row[key+'_'+str(scope)]=(1/scope if key=='占比' else scope if key=='总额' else .2)
                rows.append(row)
        p=self.root/'data/资金轮动/日表.csv';p.parent.mkdir(parents=True,exist_ok=True);pd.DataFrame(rows).to_csv(p,index=False);return rows
    def news(self,day,score=2,tag='标签',count=1):
        p=self.root/'data/新闻'/day[:7]/(day+'.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(''.join(json.dumps({'id':day+':'+str(i),'score':score,'tags':{'行业':[tag]}},ensure_ascii=False)+'\n' for i in range(count)))
    def test_flow_reads_last_revision_even_when_new_value_missing(self):
        rows=self.flow();last=dict(rows[-1],占比_31=np.nan);pd.DataFrame([last]).to_csv(self.root/'data/资金轮动/日表.csv',mode='a',header=False,index=False)
        x=sig.flow_latest(self.root);r=x[(x.date==last['date'])&(x.code==last['code'])].iloc[0]
        self.assertTrue(pd.isna(r['占比_31']));self.assertEqual(len(x),len(rows))
    def test_fixed_28_and_31_are_separate_and_new_three_never_enter_history(self):
        self.flow();x=sig.flow_latest(self.root);s=self.source();names=src.registry(self.root)
        old=sig.build(x,names,s,28);current=sig.build(x,names,s,31)
        self.assertFalse(old.code.isin(src.NEW).any());self.assertEqual(old.date.min(),'2014-02-21');self.assertEqual(current.date.min(),'2021-12-13')
        c=self.ordinary[0];day='2026-10-29'
        self.assertAlmostEqual(old[(old.code==c)&(old.date==day)].iloc[0]['资金_成交占比'],1/28)
        self.assertAlmostEqual(current[(current.code==c)&(current.date==day)].iloc[0]['资金_成交占比'],1/31)
    def test_excluded_composite_only_in_denominator_not_ranks(self):
        self.flow();table=sig.build(sig.flow_latest(self.root),src.registry(self.root),self.source(),31)
        excluded=table[table.code==self.aggregate];self.assertTrue(excluded['资金_相对沪深300_20'].isna().all());self.assertTrue(excluded['资金_相对沪深300_20_有效排名'].isna().all())
        self.assertTrue((excluded['资金_相对沪深300_20_有效行业数']==30).all());self.assertTrue(excluded['资金_成交占比'].notna().all())
    def test_full_rank_blank_when_any_candidate_missing_and_zero_is_real(self):
        self.flow();x=sig.build(sig.flow_latest(self.root),src.registry(self.root),self.source(),31);one=x[x.date=='2026-10-29'].copy();field='资金_相对沪深300_20';one.loc[one.code==self.ordinary[0],field]=np.nan
        out=sig.add_ranks(one,30);self.assertTrue(out[field+'_全排名'].isna().all());self.assertEqual(out[field+'_有效行业数'].iloc[0],29)
    def test_missing_claude_tables_fail_without_creating_empty_replacements(self):
        p=self.root/'system/行业清单.csv';p.unlink()
        with self.assertRaises(ValueError):src.registry(self.root)
        self.assertFalse(p.exists());q=self.root/'system/催化日历.csv';q.unlink()
        with self.assertRaises(ValueError):src.calendar(self.root)
        self.assertFalse(q.exists())
    def test_catalysts_with_partial_dates_are_display_only(self):
        s=self.source();self.assertEqual([x['具体日期'] for x in s.catalysts],[True,False]);r=s.for_day('2026-10-29',self.ordinary[0]);self.assertEqual(r['热度_已登记当日催化数'],1)
        self.assertIsNone(s.for_day('2026-11-01',self.ordinary[0])['热度_已登记当日催化数'])
    def test_missing_news_is_unknown_observed_no_match_is_zero(self):
        self.news('2026-10-29',tag='其他');s=self.source();self.assertEqual(s.for_day('2026-10-29',self.ordinary[0])['热度_新闻当日条数'],0)
        self.assertIsNone(s.for_day('2026-10-28',self.ordinary[0])['热度_新闻当日条数'])
        s.names[self.ordinary[1]]['新闻标签']='';self.assertIsNone(s.for_day('2026-10-29',self.ordinary[1])['热度_新闻当日条数'])
    def test_news_four_week_windows_require_all_56_dates_and_deduplicate_ids(self):
        end=dt.date(2026,10,29)
        for k in range(56):self.news((end-dt.timedelta(days=k)).isoformat(),count=3 if k<28 else 2)
        p=self.root/'data/新闻/2026-10/2026-10-29.jsonl';p.write_text(p.read_text()+p.read_text().splitlines()[0]+'\n')
        r=self.source().for_day('2026-10-29',self.ordinary[0]);self.assertEqual(r['热度_新闻近28日条数'],84);self.assertEqual(r['热度_新闻前28日条数'],56);self.assertAlmostEqual(r['热度_新闻4周变化率'],.5)
        p.unlink();self.assertIsNone(self.source().for_day('2026-10-29',self.ordinary[0])['热度_新闻4周变化率'])
    def test_concept_counts_a_row_once_and_late_capture_is_not_used(self):
        self.write('data/社区情绪/2026-10-29/同花顺热门概念.csv',['name'],[{'name':'半导体芯片'},{'name':'其他'}]);p=self.root/'data/社区情绪/2026-10-29/运行记录.txt';p.write_text('抓取时间：2026-10-29 22:38\n成功：同花顺热门概念（2行）\n失败：\n无')
        self.assertEqual(self.source().for_day('2026-10-29',self.ordinary[0])['热度_概念出现次数'],1)
        p.write_text(p.read_text().replace('2026-10-29 22:38','2026-10-30 22:38'));self.assertIsNone(self.source().for_day('2026-10-29',self.ordinary[0])['热度_概念出现次数'])
    def test_macro_revision_cannot_leak_before_its_visibility(self):
        self.write('data/宏观/派生.csv',['date','指标','value','可见时刻'],[{'date':'2026-10-29','指标':'中国10年国债','value':10,'可见时刻':'2026-10-29T18:00:00+08:00'},{'date':'2026-10-29','指标':'中国10年国债','value':999,'可见时刻':'2026-10-30T18:00:00+08:00'}])
        s=self.source();c=self.ordinary[0];s.names[c]['久期']='长';r=s.for_day('2026-10-29',c);self.assertEqual(r['环境_中国10年20日变化bp'],10);self.assertEqual(r['环境_中国久期适配bp'],-10)
        c=self.ordinary[1];s.names[c]['久期']='中';self.assertEqual(s.for_day('2026-10-29',c)['环境_中国久期适配bp'],0)
    def quarter_fixture(self,announcement=''):
        code=self.ordinary[0];self.json('data/v3/申万成分.json',{code:{'date':'2020-01-01','rows':[{'证券代码':'000001'},{'证券代码':'000002'}]}})
        self.write('data/财报/单季/000001.csv',['报告期','扣非净利润','净利润','营业总收入','公告日'],[{'报告期':d,'扣非净利润':kf,'净利润':kf,'营业总收入':rev,'公告日':announcement if d=='2026-06-30' else ''} for d,kf,rev in [('2025-03-31',100,100),('2025-06-30',100,100),('2026-03-31',130,110),('2026-06-30',160,120)]])
        return code
    def test_quarter_after_deadline_exact_v3_and_incomplete_industry_not_zero(self):
        c=self.quarter_fixture();s=self.source();self.assertIsNone(s.quarter('2026-08-31',c)['业绩_样本加速占比']);r=s.quarter('2026-09-01',c)
        self.assertEqual(r['业绩_样本加速占比'],1);self.assertEqual(r['业绩_季报覆盖率'],.5);self.assertEqual(r['业绩_可评估家数'],1);self.assertIsNone(r['业绩_利润加速占比'])
    def test_delayed_actual_announcement_and_snapshot_backcast(self):
        c=self.quarter_fixture('2026-09-05');s=self.source();self.assertIsNone(s.quarter('2026-09-05',c)['业绩_样本加速占比']);self.assertEqual(s.quarter('2026-09-06',c)['业绩_样本加速占比'],1)
        s.members[c]['date']='2026-09-10';self.assertEqual(s.quarter('2026-09-09',c)['业绩_可评估家数'],1)
    def test_industry_thresholds_and_sample_denominator(self):
        c=self.quarter_fixture();template=src.csv_rows(self.root/'data/财报/单季/000001.csv')
        for total,valid,expected in [(20,12,1),(20,11,None),(15,9,None),(16,10,1)]:
            with self.subTest(total=total,valid=valid):
                s=self.source();s.members[c]={'date':'2026-10-21','rows':[{'证券代码':str(i).zfill(6)} for i in range(total)]}
                s.quarters={str(i).zfill(6):template for i in range(valid)}
                r=s.quarter('2026-09-01',c)
                self.assertEqual(r['业绩_可评估家数'],valid);self.assertEqual(r['业绩_利润加速占比'],expected)
                self.assertEqual(r['业绩_样本加速占比'],1);self.assertEqual(r['业绩_季报覆盖率'],valid/total)
        s=self.source();s.members[c]={'date':'2026-10-21','rows':[{'证券代码':str(i).zfill(6)} for i in range(20)]}
        not_accelerating=[dict(r,扣非净利润=100) for r in template]
        s.quarters={str(i).zfill(6):template if i<6 else not_accelerating for i in range(12)}
        r=s.quarter('2026-09-01',c);self.assertEqual(r['业绩_加速家数'],6)
        self.assertEqual(r['业绩_利润加速占比'],.5);self.assertEqual(r['业绩_季报覆盖率'],.6)
    def test_new_history_overrides_cache_and_retains_legal_boundary(self):
        from earnings_history import FIELDS,visibility,deadline
        c=self.quarter_fixture();rows=src.csv_rows(self.root/'data/财报/单季/000001.csv')
        for row in rows:row.pop('公告日',None)
        self.write('data/财报/全市场单季/000001.csv',FIELDS,[dict(r,法定截止=deadline(r['报告期']).isoformat(),可见时刻=visibility(r['报告期'])) for r in rows]+[dict(rows[-1],扣非净利润='',净利润='',法定截止='2026-08-31',可见时刻=visibility('2026-06-30'))])
        s=self.source();self.assertIsNone(s.quarter('2026-09-01',c)['业绩_样本加速占比'])
    def test_known_delayed_announcement_survives_new_history_overlay(self):
        from earnings_history import FIELDS,visibility,deadline
        c=self.quarter_fixture('2026-09-05');rows=src.csv_rows(self.root/'data/财报/单季/000001.csv')
        for row in rows:row.pop('公告日',None)
        self.write('data/财报/全市场单季/000001.csv',FIELDS,[dict(r,法定截止=deadline(r['报告期']).isoformat(),可见时刻=visibility(r['报告期'])) for r in rows])
        s=self.source()
        for day in ('2026-09-01','2026-09-05'):self.assertEqual(s.quarter(day,c)['业绩_可评估家数'],0)
        self.assertEqual(s.quarter('2026-09-06',c)['业绩_可评估家数'],1)

    def test_neutral_note_and_missing_rates_keep_raw_missing(self):
        self.flow();names=src.registry(self.root);c=self.ordinary[0];names[c]['久期']='中';s=src.Sources(self.root,names)
        r=s.for_day('2026-10-29',c);self.assertEqual(r['环境_中国久期适配bp'],0);self.assertEqual(r['环境_美国久期适配bp'],0)
        self.assertIsNone(r['环境_中国10年20日变化bp']);self.assertEqual(r['环境_中国可见时刻'],'')
        table=sig.build(sig.flow_latest(self.root),names,s,28);self.assertTrue(table[table.code==c]['使用时点说明'].str.contains('中性定义，非缺失').all())
    def test_size_failure_leaves_both_csv_and_derivatives_unchanged(self):
        from unittest.mock import patch
        self.flow();sig.run(self.root,now=dt.datetime(2026,10,30,17,tzinfo=src.BJ))
        before={p:p.read_bytes() for p in (self.root/'data/主线').rglob('*') if p.is_file()}
        path=self.root/'data/资金轮动/日表.csv';frame=pd.read_csv(path,dtype={'code':str});frame.loc[0,'相对沪深300_20']=.1234;frame.to_csv(path,index=False)
        with patch('mainline_signal_store.LIMIT',1):
            with self.assertRaisesRegex(ValueError,'100MiB'):sig.run(self.root,now=dt.datetime(2026,10,30,17,tzinfo=src.BJ))
        self.assertTrue(all(p.read_bytes()==value for p,value in before.items()))

    def test_eps_year_mapping_and_observation_timestamp_are_respected(self):
        c=self.ordinary[0];self.write('data/一致预期/行业上修.csv',['日期','申万一级代码','预测年度','上修占比','可比家数'],[{'日期':'2026-10-29','申万一级代码':c,'预测年度':'2026','上修占比':0,'可比家数':5},{'日期':'2026-10-29','申万一级代码':c,'预测年度':'2027','上修占比':.4,'可比家数':5}])
        self.write('data/一致预期/2026-10/2026-10-29.csv',['抓取时间'],[{'抓取时间':'2026-10-29T10:00:00+08:00'}]);r=self.source().for_day('2026-10-29',c);self.assertEqual(r['业绩_EPS本年上修占比'],0);self.assertEqual(r['业绩_EPS次年上修占比'],.4)
        self.write('data/一致预期/2026-10/2026-10-29.csv',['抓取时间'],[{'抓取时间':'2026-10-29T20:00:00+00:00'}]);self.assertIsNone(self.source().for_day('2026-10-29',c)['业绩_EPS本年上修占比'])
    def test_latest_all_funding_values_missing_is_failure_not_successful_empty_table(self):
        self.flow();p=self.root/'data/资金轮动/日表.csv';x=pd.read_csv(p,dtype={'code':str});x.loc[x.date=='2026-10-29','占比_31']=np.nan;x.to_csv(p,index=False)
        r=sig.run(self.root,now=dt.datetime(2026,10,30,17,tzinfo=src.BJ))
        self.assertEqual(r['status'],'失败');self.assertTrue(any('资金占比均缺' in e for e in r['errors']))
    def test_full_backfill_idempotent_and_inputs_and_claude_tables_unchanged(self):
        self.flow();inputs={str(p):p.read_bytes() for p in (self.root/'system').glob('*.csv')};now=dt.datetime(2026,10,30,15,tzinfo=src.BJ)
        first=sig.run(self.root,now=now);before={p:p.read_bytes() for p in (self.root/'data/主线').rglob('*.csv')};second=sig.run(self.root,now=now+dt.timedelta(hours=1))
        self.assertTrue(all(n==0 for n in second['新增行'].values()));self.assertTrue(all(p.read_bytes()==b for p,b in before.items()));self.assertTrue(all(Path(p).read_bytes()==b for p,b in inputs.items()))
        self.assertIn('缺',(self.root/'data/主线/最新.md').read_text());self.assertNotIn('建议买入',(self.root/'data/主线/最新.md').read_text())


if __name__=='__main__':unittest.main()
