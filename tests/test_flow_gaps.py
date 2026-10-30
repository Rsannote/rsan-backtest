"""PR21缺口回归：交易日窗口、实际有效样本分母及关键列健康检查。"""
import csv
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import flow_metrics as m
import flow_source as s
import health_check as h
from repair_flow_gaps import find_gaps


class GapWindowTests(unittest.TestCase):
    def series(self):return pd.Series(np.arange(1.,301.),index=pd.bdate_range('2024-01-02',periods=300).strftime('%Y-%m-%d'))
    def test_single_middle_gap_keeps_calendar_window_and_actual_rank_denominator(self):
        x=self.series();x.iloc[275]=np.nan
        mean=m.valid_rolling(x,60);rank=m.valid_rolling(x,250,'rank')
        self.assertAlmostEqual(mean.iloc[-1],x.iloc[-60:].mean())
        x.iloc[-1]=250
        window=x.iloc[-250:].dropna();expected=(window<=250).sum()/len(window)*100
        self.assertAlmostEqual(m.valid_rolling(x,250,'rank').iloc[-1],expected)
        self.assertNotAlmostEqual(expected,(window<=250).sum()/250*100)
        self.assertTrue(np.isnan(rank.iloc[275]))
    def test_thirty_gaps_exceed_both_sixty_and_year_tolerance(self):
        x=self.series();x.iloc[265:295]=np.nan
        self.assertTrue(np.isnan(m.valid_rolling(x,60).iloc[-1]));self.assertTrue(np.isnan(m.valid_rolling(x,250,'rank').iloc[-1]))
        # 不能把缺失日删掉后，从窗口外借旧数据凑60/250个有效点。
        self.assertEqual(x.iloc[-250:].notna().sum(),220)
    def test_exact_ninety_percent_boundary_and_warmup(self):
        for window,allowed in [(20,2),(60,6),(250,25)]:
            x=self.series();x.iloc[-allowed-1:-1]=np.nan
            self.assertTrue(np.isfinite(m.valid_rolling(x,window).iloc[-1]))
            x.iloc[-allowed-2]=np.nan;self.assertTrue(np.isnan(m.valid_rolling(x,window).iloc[-1]))
        x=self.series();self.assertTrue(np.isnan(m.valid_rolling(x,250,'rank').iloc[248]));self.assertEqual(m.valid_rolling(x,250,'rank').iloc[249],100)
    def test_no_nan_zero_fill_no_current_value_no_rank_and_ties(self):
        x=self.series();x.iloc[-1]=np.nan;self.assertTrue(np.isnan(m.valid_rolling(x,250,'rank').iloc[-1]))
        x[:]=2;x.iloc[275]=np.nan;self.assertEqual(m.valid_rolling(x,250,'rank').iloc[-1],100)
    def test_daily_notes_and_ma_use_observed_values(self):
        names={f'801{i:03d}':'合成' for i in range(28)}|{c:'合成新行业' for c in s.NEW};x=self.series()
        C=pd.DataFrame({c:x.copy() for c in names});A=C.copy();C.iloc[275]=np.nan;A.iloc[275]=np.nan
        B=pd.Series(1.,index=C.index);daily,metrics=m.compute_sw(C,A,B,names);r=daily[daily.code==sorted(names)[0]].iloc[-1]
        self.assertIn('收盘60日窗口内缺 1 天',r['数据说明']);self.assertIn('占比_31：250日窗口内缺 1 天',r['数据说明'])
        self.assertAlmostEqual(r['偏离60日线'],300/C.iloc[-60:,0].mean()-1);self.assertTrue(np.isfinite(r['占比一年分位_31']))
    def test_structural_new_three_are_not_pre_2021_gaps(self):
        names={'old':'旧行业',**{c:'新行业' for c in s.NEW}};index=pd.Index(['2021-12-10','2021-12-13']);C=pd.DataFrame(1.,index=index,columns=names);A=C.copy()
        A.loc[index[0],list(s.NEW)]=np.nan;C.loc[index[0],list(s.NEW)]=np.nan
        self.assertEqual(find_gaps(C,A,names),{})
        A.loc[index[1],'old']=np.nan;self.assertEqual(find_gaps(C,A,names),{index[1]:['old']})
    def test_gap_repair_reason_is_recorded_and_old_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'raw.csv';s.append_rows(p,s.RAW,[{'date':'2024-01-02','close':1,'amount':1}],['date'],'a');old=p.read_bytes()
            s.append_rows(p,s.RAW,[{'date':'2024-01-03','close':2,'amount':2}],['date'],'b',revision_reason='缺口真实重抓修订')
            self.assertTrue(p.read_bytes().startswith(old));self.assertEqual(list(csv.DictReader(p.open()))[-1]['修订原因'],'缺口真实重抓修订')


class KeyCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.policy={'path':'daily.csv','columns':['year','ma'],'minimum':.9,'expected_count':10}
    def write(self,rows):
        with (self.root/'daily.csv').open('w',newline='') as f:w=csv.DictWriter(f,['date','code','year','ma']);w.writeheader();w.writerows(rows)
    def rows(self):return [{'date':'2026-10-29','code':str(i),'year':0,'ma':1} for i in range(10)]
    def test_zero_is_valid_ninety_percent_passes_and_nan_inf_fail(self):
        rows=self.rows();rows[0]['year']='nan';self.write(rows);coverage,issues=h.key_value_coverage(self.root,self.policy,'2026-10-29');self.assertEqual(coverage['year']['比例'],.9);self.assertEqual(issues,[])
        rows[1]['year']='inf';self.write(rows);coverage,issues=h.key_value_coverage(self.root,self.policy,'2026-10-29');self.assertEqual(coverage['year']['比例'],.8);self.assertEqual(len(issues),1)
    def test_missing_whole_rows_count_in_denominator_and_wrong_date_not_used(self):
        self.write(self.rows()[:2]);coverage,issues=h.key_value_coverage(self.root,self.policy,'2026-10-29');self.assertEqual(coverage['year']['比例'],.2)
        coverage,issues=h.key_value_coverage(self.root,self.policy,'2026-10-30');self.assertEqual(coverage['ma']['比例'],0);self.assertEqual(len(issues),2)
    def test_last_revision_wins(self):
        rows=self.rows();rows.extend([dict(r,year='',ma='') for r in rows[:2]]);self.write(rows)
        coverage,issues=h.key_value_coverage(self.root,self.policy,'2026-10-29');self.assertEqual(coverage['year']['有值'],8)
    def test_successful_program_with_empty_key_columns_is_partial_failure(self):
        rows=self.rows()
        for r in rows:r['year']=''
        self.write(rows);report={'data_date':'2026-10-29','generated_at':'2026-10-30T14:00:00+08:00','status':'正常','errors':[]}
        (self.root/'report.json').write_text(json.dumps(report))
        item={'id':'flow','name':'资金轮动','kind':'judgment_report','check_mode':'previous_trading_day','outputs':['report.json'],'key_value_coverage':self.policy}
        actual,issues,success=h.load_actual(self.root,item,{'data_date':'2026-10-29'})
        self.assertEqual(success,1);self.assertTrue(issues);self.assertEqual(actual['关键列有值比例']['year']['比例'],0)
        with patch.object(h,'expectation',return_value={'data_date':'2026-10-29'}):
            result=h.check_item(self.root,item,dt.datetime(2026,10,30,14,30,tzinfo=h.BJ),None)
        self.assertEqual(result['status'],'部分失败')


if __name__=='__main__':unittest.main()
