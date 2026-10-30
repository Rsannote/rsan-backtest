"""T6b 离线合成夹具：时间可见性、原文保护、官方口径与固定窗口。"""
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import update_macro as macro
from auto_judgment_common import BJ, TradingCalendar


class MacroTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        path=self.root/'system/交易所休市.json';path.parent.mkdir()
        path.write_text(json.dumps({'coverage_start':'2026-01-01','coverage_end':'2027-12-31','closed_dates':['2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07']}))
        path=self.root/'data/行情库/指数/sh000300.csv';path.parent.mkdir(parents=True)
        path.write_text('date,close,factor\n2015-01-02,100,1\n2026-10-29,100,1\n')
        self.calendar=TradingCalendar(self.root)
        self.now=dt.datetime(2026,10,29,20,tzinfo=BJ)

    def test_visibility_same_day_time_and_holiday_next_session(self):
        self.assertEqual(macro.visible_at('中国10年国债','2026-10-29',self.calendar).hour,18)
        self.assertEqual(macro.visible_at('美元兑人民币','2026-10-29',self.calendar).time(),dt.time(9,15))
        self.assertEqual(macro.visible_at('美国10年国债','2026-09-30',self.calendar).isoformat(),'2026-10-08T09:30:00+08:00')
        self.assertEqual(macro.visible_at('COMEX黄金','2026-10-30',self.calendar).date(),dt.date(2026,11,2))
        self.assertEqual(macro.visible_at('M1','2026-12',self.calendar).isoformat(),'2027-01-15T23:59:59+08:00')
        self.assertEqual(macro.visible_at('制造业PMI','2026-09',self.calendar).isoformat(),'2026-10-01T23:59:59+08:00')

    def test_append_revision_asof_and_exact_original_bytes(self):
        path=self.root/'test.csv'
        content,n=macro.prepare(path,'中国10年国债',[('2026-10-28',2.0,'https://official.test/')],self.calendar,self.now)
        path.write_bytes(content.replace('\n','\r\n').encode());before=path.read_bytes()
        changed,n=macro.prepare(path,'中国10年国债',[('2026-10-28',2.1,'https://official.test/')],self.calendar,self.now)
        self.assertTrue(changed.encode().startswith(before));self.assertEqual(n,1)
        rows=list(csv.DictReader(io.StringIO(changed)))
        self.assertEqual(len(rows),2)
        self.assertEqual(macro.asof(rows,self.now-dt.timedelta(hours=1))[-1]['value'],'2.0')
        self.assertEqual(macro.asof(rows,self.now)[-1]['value'],'2.1')
        path.write_bytes(changed.encode());again,n=macro.prepare(path,'中国10年国债',[('2026-10-28',2.1,'https://official.test/')],self.calendar,self.now)
        self.assertEqual(n,0);self.assertEqual(again,changed)

    def test_future_visible_row_is_not_latest_and_precision_idempotent(self):
        path=self.root/'test.csv';value=1234.56789012345
        content,n=macro.prepare(path,'美国10年国债',[('2026-10-29',value,'https://official.test/')],self.calendar,self.now)
        rows=list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(macro.asof(rows,self.now),[])
        path.write_text(content)
        self.assertEqual(macro.prepare(path,'美国10年国债',[('2026-10-29',value,'https://official.test/')],self.calendar,self.now)[1],0)

    def test_currency_identity_and_not_mixing_fred(self):
        source='https://www.chinamoney.com.cn/test'
        obj={'head':{'rep_code':200},'data':{'lastDate':'2026-10-29 9:15'},'records':[{'vrtEName':'EUR/CNY','vrtName':'欧元/人民币','price':'8'},{'vrtEName':'USD/CNY','vrtName':'美元/人民币','price':'7'}]}
        self.assertEqual(macro.cfets(json.dumps(obj),source)[0][:2],('2026-10-29',7.0))
        obj['records']=obj['records'][:1]
        with self.assertRaises(ValueError):macro.cfets(json.dumps(obj),source)
        visible=macro.visible_at('美元兑人民币_FRED历史补充','2026-10-19',self.calendar)
        self.assertEqual(visible.date(),dt.date(2026,10,27))
        self.assertIn('不是中间价',macro.note_for('美元兑人民币_FRED历史补充'))

    def test_official_parser_sign_units_and_nominal_not_tips(self):
        self.assertEqual(macro.official_month('狭义货币（M1）余额116.76万亿元','M1')[:2],(1167600.0,50.0))
        self.assertEqual(macro.official_month('全国工业生产者出厂价格同比下降2.3%','PPI同比')[0],-2.3)
        self.assertEqual(macro.official_month('全国居民消费价格同比持平','CPI同比')[0],0)
        self.assertEqual(macro.official_month('制造业采购经理指数（ PMI ）为49.8%','制造业PMI')[0],49.8)
        header='<tr><th>Instruments</th>'+''.join(f'<th>2026<br>Oct<br>{d}</th>' for d in (22,23,26,27,28))+'</tr>'
        text='<table>'+header+'<tr><td>10-year</td>'+''.join('<td>4.0</td>' for _ in range(5))+'</tr><tr><td>10-year</td>'+''.join('<td>2.0</td>' for _ in range(5))+'</tr></table>'
        self.assertEqual(set(macro.h15(text).values()),{4.0})
        with self.assertRaises(ValueError):macro.official_month('非制造业商务活动指数为50.1%','制造业PMI')

    def test_guard_empty_duplicates_nan_and_network(self):
        for points in ([],[('2026-10-29',float('nan'),'https://x/')],[('2026-10-29',1,'https://x/'),('2026-10-29',2,'https://x/')]):
            with self.assertRaises(ValueError):macro.validated(points)
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}):
            with self.assertRaises(RuntimeError):macro.Client().get('https://example.invalid')

    def test_official_failure_leaves_existing_raw_csv_untouched(self):
        output=self.root/'data/宏观';output.mkdir(parents=True)
        path=output/'中国10年国债.csv'
        before=macro.prepare(path,'中国10年国债',[('2026-10-28',2.0,'https://official.test/')],self.calendar,self.now)[0].encode()
        path.write_bytes(before)
        points=[('2015-01-02',2.0,'https://official.test/'),('2026-10-29',2.1,'https://official.test/')]
        class Fake:
            trace=[]
            def get(self,unused):return '{}'
        with patch.object(macro,'fetch_cn',return_value=points),patch.object(macro,'fetch_fred',return_value=points),patch.object(macro,'cfets',return_value=points),patch.object(macro,'fetch_yahoo',return_value=points),patch.object(macro,'em_points',return_value=points),patch.object(macro,'verify_sources',side_effect=ValueError('官方原文不可达')):
            report=macro.run(self.root,verify=True,client=Fake(),now=self.now)
        self.assertEqual(report['status'],'失败')
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse((output/'美国10年国债.csv').exists())
        self.assertIn('官方原文不可达',report['errors'][0])

    def test_health_manifest_reads_macro_errors_not_observation_dates(self):
        import health_check
        item=next(i for i in json.loads((Path(__file__).resolve().parents[1]/'system/运行清单.json').read_text())['items'] if i['id']=='macro')
        path=self.root/item['outputs'][0];path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'data_date':'2026-10-29','generated_at':self.now.isoformat(),'status':'失败','errors':['中债源超时'],'sources':{'M1':{'最新观测':'2026-09'}}}))
        actual,issues,success=health_check.load_actual(self.root,item,{'data_date':'2026-10-29'})
        self.assertEqual(actual['data_date'],'2026-10-29');self.assertIn('中债源超时',issues);self.assertEqual(success,0)

    def test_yahoo_uses_local_session_and_excludes_unfinished_quote(self):
        class Fake:
            def get(self,unused):
                return json.dumps({'chart':{'result':[{'meta':{'symbol':'GC=F','exchangeTimezoneName':'America/New_York'},'timestamp':[1793192400,1793278800], 'indicators':{'quote':[{'close':[4000,4100]}]}}]}})
        # 两个时间戳只为合成夹具，按交易所当地日期过滤当前未收盘报价。
        now=dt.datetime(2026,10,30,8,tzinfo=BJ)
        points=macro.fetch_yahoo(Fake(),'GC=F',now)
        self.assertTrue(all(d<='2026-10-29' for d,_,_ in points))

    def test_chinabond_before_eighteen_does_not_request_empty_today_tail(self):
        from urllib.parse import urlparse,parse_qs
        requested=[]
        class Fake:
            def get(self,address):
                q=parse_qs(urlparse(address).query);requested.append(q)
                d=q['endDate'][0]
                return '<table><tr><th>曲线名称</th><th>日期</th><th>10年</th></tr><tr><td>中债国债收益率曲线</td><td>'+d+'</td><td>2.0</td></tr></table>'
        now=dt.datetime(2026,10,30,9,tzinfo=BJ)
        points=macro.fetch_cn(Fake(),self.root,now,full=True)
        self.assertEqual(points[-1][0],'2026-10-29')
        self.assertEqual(requested[-1]['startDate'],['2026-01-01'])
        self.assertTrue(all(q['endDate'][0]<'2026-10-30' for q in requested))

    def test_derived_fixed_twenty_a_sessions_and_no_future_observation(self):
        # 自备覆盖日历，防止源数据缺日被误当A股休市。
        self.calendar.official={'coverage_start':'2015-01-01','coverage_end':'2027-12-31'}
        rows=[]
        for date,value in [('2026-09-01',2.0),('2026-09-30',2.3),('2026-10-29',9.0)]:
            visible=macro.visible_at('中国10年国债',date,self.calendar)
            rows.append({'date':date,'value':str(value),'source':'https://official.test','可见时刻':visible.isoformat(),'记录时间':self.now.isoformat()})
        now=dt.datetime(2026,10,29,12,tzinfo=BJ)
        result=macro.derived({'中国10年国债':rows,'美国10年国债':rows},self.calendar,now)
        last=next(r for r in reversed(result) if r['指标']=='中国10年国债')
        self.assertEqual(last['date'],'2026-10-28');self.assertNotEqual(last['终点观测日'],'2026-10-29')
        self.assertEqual(len(self.calendar.window(last['起点A股日'],20)),21)


if __name__=='__main__':unittest.main()
