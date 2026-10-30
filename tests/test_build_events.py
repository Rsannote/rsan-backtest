"""T9 合成价格、时点数据、缺失与追加保护；全程离线。"""
import copy
import csv
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import build_events as events


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.now=dt.datetime(2026,10,29,20,tzinfo=events.BJ)
    def dates(self,n):return [(dt.date(2026,1,1)+dt.timedelta(days=i)).isoformat() for i in range(n)]
    def write(self,path,fields,rows):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True)
        with p.open('w',newline='') as f:
            w=csv.writer(f);w.writerow(fields);w.writerows(rows)
    def fixture(self):
        days=[(dt.date(2026,1,1)+dt.timedelta(days=i)).isoformat() for i in range(180) if (dt.date(2026,1,1)+dt.timedelta(days=i)).weekday()<5]
        self.write('data/行情库/指数/sh000300.csv',['date','close','factor'],[(d,100+i,1) for i,d in enumerate(days)])
        self.write('data/市场状态/日指标.csv',['date','成交额亿','来源'],[(d,100+i,'合成') for i,d in enumerate(days)])
        self.write('data/市场状态/融资余额.csv',['date','融资余额亿'],[(d,200+i) for i,d in enumerate(days)])
        self.write('data/社区情绪/温度计.csv',['date','温度'],[(d,40) for d in days])
        for name in ('中国10年国债','美国10年国债'):
            self.write('data/宏观/'+name+'.csv',['date','value','可见时刻','记录时间'],[(days[0],2,days[0]+'T18:00:00+08:00',days[0]+'T18:00:00+08:00'),(days[-1],9,'2026-12-01T18:00:00+08:00','2026-12-01T18:00:00+08:00')])
        return days
    def test_exact_threshold_and_confirmation_not_extreme_date(self):
        rows=events.episodes(self.dates(9),[100,85,90,98,120,140,119,80,92])
        first=rows[0];self.assertEqual((first['start'],first['end'],first['formed'],first['confirmed']),(0,1,1,3))
        self.assertAlmostEqual(first['amplitude'],-.15);self.assertEqual(first['duration'],1)
        self.assertEqual(rows[1]['kind'],'up');self.assertEqual(rows[1]['confirmed'],6)
        self.assertIsNone(rows[-1]['confirmed'])
    def test_nested_recovery_creates_new_local_event_without_new_all_time_high(self):
        rows=events.episodes(self.dates(7),[100,80,92,90,70,81,80])
        down=[r for r in rows if r['kind']=='down']
        self.assertEqual([r['start'] for r in down],[0,2]);self.assertEqual(down[1]['end'],4)
    def test_no_false_small_move_or_nan_and_duplicate_dates(self):
        self.assertEqual(events.episodes(self.dates(3),[100,99,101]),[])
        for days,values in [(['2026-01-01']*2,[100,80]),(self.dates(2),[100,float('nan')]),(self.dates(2),[100,0])]:
            with self.assertRaises(ValueError):events.episodes(days,values)
    def test_plateau_keeps_first_peak_and_first_trough(self):
        rows=events.episodes(self.dates(6),[100,100,80,80,92,95])
        self.assertEqual((rows[0]['start'],rows[0]['end']),(0,2))
    def test_features_use_past_macro_and_lagged_margin(self):
        days=self.fixture();data=events.Data(self.root,self.now);r=data.features(len(days)-1)
        self.assertEqual(r['启动时中国10年国债'],2)
        self.assertEqual(r['启动时融资余额亿'],200+len(days)-2)
        self.assertAlmostEqual(r['启动时融资余额变化'],(200+len(days)-2)/(200+len(days)-7)-1)
        self.assertAlmostEqual(r['启动前60日涨幅'],(100+len(days)-1)/(100+len(days)-61)-1)
        self.assertEqual(json.loads(r['特征来源'])['融资余额']['date'],days[-2])
        self.assertEqual(r['启动时成交额分位'],100)
    def test_missing_history_not_zero_and_current_not_future_day(self):
        days=self.fixture();data=events.Data(self.root,dt.datetime.fromisoformat(days[-1]+'T12:00:00').replace(tzinfo=events.BJ))
        self.assertEqual(data.days[-1],days[-2])
        r=data.features(0);self.assertIsNone(r['启动前60日涨幅']);self.assertIsNone(r['启动时融资余额变化']);self.assertIsNone(r['启动时成交额分位'])
    def test_standard_euclidean_fitted_on_history_and_complete_cases(self):
        rows=[]
        for i,value in enumerate((0,2)):
            row={f:value for f in events.FEATURES};row.update({'事件id':str(i),'类型':'大调整','开始':'a','结束':'b','状态':'已确认结束'});rows.append(row)
        current={f:1 for f in events.FEATURES}
        ranked,excluded,scale=events.distances(current,rows)
        self.assertAlmostEqual(ranked[0]['距离'],len(events.FEATURES)**.5)
        self.assertTrue(all(v==1 for v in scale['均值'].values()))
        bad=copy.deepcopy(rows[0]);bad['事件id']='缺';bad[events.FEATURES[0]]=None
        ranked,excluded,_=events.distances(current,rows+[bad]);self.assertEqual(len(ranked),2);self.assertEqual(len(excluded),1)
        rows[0]['状态']='待反向确认'
        ranked,excluded,scale=events.distances(current,rows)
        self.assertEqual(ranked,[]);self.assertEqual(len(excluded),1)
        self.assertIn('无法标准化',scale['原因'])
    def test_current_missing_disables_ranking_and_constant_dimension_zero(self):
        row={f:1 for f in events.FEATURES};row.update({'事件id':'x','类型':'大反弹','开始':'a','结束':'b','状态':'已确认结束'})
        ranked,_,scale=events.distances({f:2 for f in events.FEATURES},[row]);self.assertEqual(ranked,[]);self.assertIn('无法标准化',scale['原因'])
        current={f:1 for f in events.FEATURES};current[events.FEATURES[0]]=None
        self.assertEqual(events.distances(current,[row])[0],[])
    def test_append_preserves_crlf_human_text_and_idempotence(self):
        path=self.root/'events.csv';row={k:'' for k in events.FIELDS};row.update({'事件id':'EVT-x','类型':'大调整','幅度':-.2,'程序版本':'合成'})
        events.append_records(path,[row],self.now)
        with path.open(newline='') as f:records=list(csv.DictReader(f))
        records[0]['触发事件']='人工原文';stream=io.StringIO(newline='');writer=csv.DictWriter(stream,events.FIELDS,lineterminator='\r\n');writer.writeheader();writer.writerows(records)
        path.write_bytes(stream.getvalue().encode());before=path.read_bytes()
        self.assertEqual(events.append_records(path,[row],self.now),0);self.assertEqual(path.read_bytes(),before)
        row['幅度']=-.3;events.append_records(path,[row],self.now)
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertEqual(list(csv.DictReader(io.StringIO(path.read_text())))[-1]['触发事件'],'人工原文')
    def test_real_library_required_years_human_columns_and_top_five(self):
        data=events.Data(ROOT,self.now)
        rows=[events.record(data,e,.15) for e in events.episodes(data.days,data.values) if e['kind']=='down' or e['amplitude']>=.2-1e-12]
        declines=[r for r in rows if r['类型']=='大调整']
        for year in ('2008','2015','2018','2022'):
            self.assertTrue(any(r['开始'][:4]<=year<=r['结束'][:4] for r in declines),year)
        self.assertTrue(all(not r[k] for r in rows for k in events.HUMAN))
        self.assertTrue(all(r['幅度']<=-.15+1e-10 for r in declines))
        self.assertTrue(all(r['幅度']>=.20-1e-10 for r in rows if r['类型']=='大反弹'))
        ranked,_,_=events.distances(data.features(len(data.days)-1),rows);self.assertGreaterEqual(len(ranked),5)


if __name__=='__main__':unittest.main()
