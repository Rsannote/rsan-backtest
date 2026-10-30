"""T8b合成数据验证，不联网，不改真实源数据。"""
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import flow_source as s
import flow_metrics as m
import flow_stats as st
import flow_indicators as f


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.codes=sorted({f'801{i:03d}' for i in range(28)}|s.NEW);self.old=sorted(set(self.codes)-s.NEW);self.names={c:'合成'+c for c in self.codes}
    def test_fixed_28_and_31_do_not_splice_or_include_new_three(self):
        index=pd.Index(['2021-12-10','2021-12-13','2021-12-14']);A=pd.DataFrame(1.,index=index,columns=self.codes);A.loc[:,sorted(s.NEW)]=2
        a,total=m.share_matrix(A,self.old,s.START);b,t31=m.share_matrix(A,self.codes,s.START31)
        self.assertAlmostEqual(total.iloc[-1],28);self.assertAlmostEqual(t31.iloc[-1],34)
        self.assertTrue(a[list(s.NEW)].isna().all().all());self.assertTrue(b.iloc[0].isna().all())
        self.assertAlmostEqual(a.iloc[-1].sum(),1);self.assertAlmostEqual(b.iloc[-1].sum(),1)
        A.loc[index[-1],self.old[0]]=np.nan;a,total=m.share_matrix(A,self.old,s.START);self.assertTrue(a.iloc[-1].isna().all())
    def test_strict_lags_percentiles_relative_returns_and_ma60(self):
        index=pd.bdate_range('2022-01-03',periods=270).strftime('%Y-%m-%d');C=pd.DataFrame({c:100+np.arange(270) for c in self.codes},index=index);A=C.copy();A.loc[:,self.old[1:]]=1
        benchmark=pd.Series(100.,index=index);daily,metrics=m.compute_sw(C,A,benchmark,self.names)
        code=self.old[0];last=daily[daily.code==code].iloc[-1]
        self.assertAlmostEqual(last['相对沪深300_20'],369/349-1);self.assertAlmostEqual(last['偏离60日线'],369/339.5-1)
        self.assertAlmostEqual(last['占比一年分位_28'],100);self.assertTrue(metrics['占比一年分位_28'].iloc[248].isna().all())
        self.assertAlmostEqual(last['占比5日变化_28'],metrics['占比_28'][code].iloc[-1]-metrics['占比_28'][code].iloc[-6])
    def test_rank_requires_full_universe_and_ties_use_code(self):
        x=pd.DataFrame({'b':[1,np.nan],'a':[1,2]});r=m.rank_complete(x,['b','a'])
        self.assertEqual((r.loc[0,'a'],r.loc[0,'b']),(1,2));self.assertTrue(r.iloc[1].isna().all())
    def test_raw_parser_identity_duplicates_missing_and_nonfinite(self):
        row={'swindexcode':'801010','bargaindate':'2026-10-29','closeindex':10,'bargainsum':2,'bargainamount':3}
        self.assertEqual(s.parse_raw({'data':[row]},'801010')[0]['amount'],2)
        for values in ([row,row],[dict(row,swindexcode='801020')],[dict(row,bargainsum=None)],[dict(row,closeindex=float('inf'))]):
            with self.assertRaises(ValueError):s.parse_raw({'data':values},'801010')
    def test_append_prefix_crlf_and_idempotence(self):
        path=self.root/'raw.csv';row={'date':'2026-10-29','close':10,'amount':2}
        s.append_rows(path,s.RAW,[row],['date'],'first');path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'));before=path.read_bytes()
        self.assertEqual(s.append_rows(path,s.RAW,[row],['date'],'later'),0);self.assertEqual(before,path.read_bytes())
        row['amount']=3;self.assertEqual(s.append_rows(path,s.RAW,[row],['date'],'later'),1);self.assertTrue(path.read_bytes().startswith(before))
    def test_source_network_guard_before_openssl(self):
        with patch.dict('os.environ',{'GITHUB_ACTIONS':'false'}):
            with self.assertRaises(RuntimeError):s.verify_tls(self.root,self.root/'tls')
    def test_distribution_zero_is_not_a_win_and_missing_not_zero(self):
        r=st.distribution([.1,-.1,0,np.nan]);self.assertEqual(r['样本数'],3);self.assertEqual(r['中位数'],0);self.assertAlmostEqual(r['胜率'],1/3)
        self.assertIsNone(st.distribution([])['胜率'])
    def test_entry_condition_and_forward_conditional_count(self):
        index=pd.bdate_range('2022-01-03',periods=40).strftime('%Y-%m-%d');names={'a':'合成A','b':'合成B'}
        C=pd.DataFrame({'a':100+np.arange(40),'b':100+np.arange(40)},index=index);B=pd.Series(100.,index=index)
        rank=pd.DataFrame(6.,index=index,columns=['a','b']);rank.loc[index[5],'a']=4;rank.loc[index[6],'b']=3
        report,pairs=st.describe(C,B,names,{'占比5日变化排名_28':rank})
        perf=next(r for r in report['事件表现'] if r['code']=='a' and r['后续交易日']==5)
        self.assertEqual(perf['样本数'],1);self.assertAlmostEqual(perf['均值'],110/105-1)
        pair=pairs[(pairs.A代码=='a')&(pairs.B代码=='b')&(pairs.后续窗口==5)].iloc[0]
        self.assertEqual(pair['条件样本'],1);self.assertEqual(pair['B随后进入次数'],1);self.assertEqual(pair['条件概率'],1)
        self.assertFalse((pairs.A代码==pairs.B代码).any())
    def write(self,name,fields,rows):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('w',newline='') as out:w=csv.writer(out);w.writerow(fields);w.writerows(rows)
    def fixture(self):
        index=pd.bdate_range('2021-01-04',periods=300).strftime('%Y-%m-%d');last=index[-1]
        self.write('data/行情库/指数/sh000300.csv',['date','close','factor'],[(d,100,1) for d in index])
        self.write('data/市场状态/日指标.csv',['date','成交额亿','来源'],[(d,31,'合成') for d in index])
        self.write('data/标的清单.csv',['code','name','group'],[('510000','合成ETF','行业ETF')])
        self.write('data/行情库/ETF/510000.csv',['date','close','factor','volume'],[(d,1,1,100) for d in index])
        base=self.root/'data/资金轮动/原始';s.append_rows(base/'行业清单.csv',s.REGISTRY,[{'code':c,'name':self.names[c]} for c in self.codes],['code'],'合成')
        for code in self.codes:
            rows=[{'date':d,'close':100+i,'amount':1} for i,d in enumerate(index) if code not in s.NEW or d>=s.START31]
            s.append_rows(base/'申万'/f'{code}.csv',s.RAW,rows,['date'],'合成')
        return last
    def test_tls_failure_uses_etf_columns_even_when_cached_sw_complete(self):
        last=self.fixture();now=dt.datetime.fromisoformat(last+'T17:00:00+08:00')
        with patch.object(f,'fetch_all',side_effect=ValueError('模拟链验证失败')):
            r=f.run(self.root,now=now)
        self.assertEqual(r['资金来源'],'ETF 近似');self.assertEqual(r['status'],'失败');self.assertEqual(r['申万'],[])
        self.assertTrue(r['ETF备用']);self.assertIn('ETF_占比',r['ETF备用'][0]);self.assertNotIn('占比_31',r['ETF备用'][0]);self.assertFalse(r['本次在线TLS验证通过'])
        alert=json.loads((self.root/'logs/资金轮动_告警.json').read_text());self.assertEqual(alert['items'][0]['status'],'失败')
    def test_new_three_only_enter_daily_from_2021_breakpoint(self):
        last=self.fixture();B,names,C,A=m.read_inputs(self.root,last);daily,_=m.compute_sw(C,A,B,names)
        self.assertTrue((daily[daily.code.isin(s.NEW)].date>=s.START31).all())
        self.assertTrue(daily[daily.code.isin(s.NEW)]['占比_28'].isna().all())


if __name__=='__main__':unittest.main()
