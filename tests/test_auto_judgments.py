"""T4合成夹具：午夜日期、草案隔离、固定交易窗口、档案原文保护；不联网。"""
import copy
import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/"scripts"))
import auto_judgments as generate
import auto_review as review
import auto_judgment_common as common
import check_judgments as checker


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.now=dt.datetime(2026,10,23,0,15,tzinfo=common.BJ)
        self.days=['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23','2026-10-26','2026-10-27','2026-10-28','2026-10-29','2026-10-30']
        self.put('system/交易所休市.json',{'coverage_start':'2026-09-01','coverage_end':'2026-12-31','closed_dates':['2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07']})
        self.csv('data/行情库/指数/sh000300.csv',[(d,100-i,1) for i,d in enumerate(self.days)])
        self.csv('data/行情库/指数/sh000001.csv',[(d,100,1) for d in self.days])
        self.csv('data/行情库/ETF/512880.csv',[(d,100+i,1) for i,d in enumerate(self.days)])
        self.config=json.loads((REPO/'system/自动判断规则.json').read_text())
        # 保留T4原框架的独立草案夹具，不随生产审批开关漂移。
        self.config['rules']=[r for r in self.config['rules'] if r['id']!='market_defend_risk']
        for rule in self.config['rules']:
            if not rule.get('reserved'):
                rule.update(enabled=False,approved=False,approval=None,status='草案待审核',to_trading_days=2)
        self.put('system/自动判断规则.json',self.config)
        self.market('2026-10-22')
        self.temp('2026-10-21,30\n2026-10-22,10\n')

    def put(self,path,obj):
        p=self.root/path; p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(obj,ensure_ascii=False),encoding='utf8')

    def csv(self,path,rows):
        p=self.root/path; p.parent.mkdir(parents=True,exist_ok=True)
        with p.open('w',newline='') as f:
            w=csv.writer(f); w.writerow(['date','close','factor']); w.writerows(rows)

    def market(self,day,state='守'):
        self.put('data/市场状态/'+day[:7]+'/'+day+'.json',{'数据日期':day,'版本':'合成v1','市场':{'结论':state},
                 '板块':{'资金流入前5':['证券'],'明细':{'证券':{'code':'512880','成交占比变化':2.5}}}})

    def temp(self,content):
        path=self.root/'data/社区情绪/温度计.csv'; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('date,温度\n'+content)

    def rule(self,id):
        return next(r for r in self.config['rules'] if r['id']==id)

    def record(self,kind='指数涨跌',code='sh000300',op='<',value=0,days=2):
        r=copy.deepcopy(self.rule('market_defend')); r['to_trading_days']=days; r['op']=op; r['value']=value
        event={'type':kind,'code':code,'name':'合成指数','source':'data/市场状态/2026-10/2026-10-22.json','text':'2026-10-22合成证据','direction':'守'}
        record=generate.make_record(r,event,'2026-10-22','2026-10-23',common.TradingCalendar(self.root))
        self.assertEqual(checker.check_record(record,'夹具'),[])
        return record

    def test_default_disabled_no_formal_writes_and_logs_all_rules(self):
        result=generate.run(self.root,'市场状态','2026-10-22',now=self.now)
        self.assertEqual(result['status'],'正常')
        self.assertEqual(len(result['skipped']),7)
        self.assertFalse(list((self.root/'judgments').glob('*.jsonl')))
        self.assertTrue(all(not r['enabled'] for r in result['rules']))

    def test_replay_only_and_reserved_cannot_be_forced(self):
        with self.assertRaises(ValueError): generate.run(self.root,include_drafts=True,now=self.now)
        with self.assertRaises(ValueError): generate.run(self.root,replay=True,include_drafts=True,out_dir=self.root/'judgments',now=self.now)
        report=generate.run(self.root,replay=True,include_drafts=True,now=self.now)
        stats={s['rule']:s for s in report['statistics']}
        self.assertEqual(stats['mainline_confirm']['events'],0)
        self.assertEqual(stats['mainline_retreat']['events'],0)
        self.assertEqual(stats['market_defend']['events'],1)
        self.assertEqual(stats['market_defend']['matured'],0)
        self.assertIsNone(stats['market_defend']['hit_rate'])
        path=self.root/'logs/test/T4/judgments/2026-10.jsonl'
        records=[json.loads(line) for line in path.read_text().splitlines()]
        self.assertTrue(all(r['date']=='2026-10-23' and r['check']['from']=='2026-10-22' for r in records))
        proxy=next(r for r in records if r['module']=='资金轮动')
        self.assertIn('代理',proxy['claim']); self.assertIn('非真实资金流入',proxy['claim'])
        self.assertFalse((self.root/'judgments').exists())

    def test_midnight_idempotence_and_rule_revision(self):
        rule=self.rule('market_defend'); rule.update(enabled=True,approved=True,approval='合成批准夹具')
        self.put('system/自动判断规则.json',self.config)
        generate.run(self.root,'市场状态','2026-10-22',now=self.now)
        path=self.root/'judgments/2026-10.jsonl'; before=path.read_bytes()
        record=json.loads(before); self.assertEqual(record['id'],'J-2026-10-23-001')
        self.assertEqual(record['check']['from'],'2026-10-22')
        generate.run(self.root,'市场状态','2026-10-22',now=self.now+dt.timedelta(days=1))
        self.assertEqual(path.read_bytes(),before)
        rule['version']='合成v2'; self.put('system/自动判断规则.json',self.config)
        generate.run(self.root,'市场状态','2026-10-22',now=self.now)
        self.assertEqual(len(path.read_text().splitlines()),2)

    def test_stale_latest_signal_cannot_create_hindsight_formal_forecast(self):
        rule=self.rule('market_defend'); rule.update(enabled=True,approved=True,approval='合成批准夹具')
        self.put('system/自动判断规则.json',self.config)
        self.put('data/市场状态/最新.json',{'数据日期':'2026-10-22'})
        report=generate.run(self.root,'市场状态',now=self.now+dt.timedelta(days=4))
        self.assertEqual(report['status'],'失败')
        self.assertIn('只允许隔离回放',report['errors'][0])
        self.assertFalse(list((self.root/'judgments').glob('*.jsonl')))

    def test_unapproved_enable_and_reserved_enable_fail_closed(self):
        self.rule('market_defend')['enabled']=True
        with self.assertRaises(ValueError): generate.validate_rules(self.config)

    def test_global_sequence_all_files_and_midnight_source_date(self):
        record=self.record()
        old=copy.deepcopy(record); old.update(id='J-2026-10-23-009',auto_key='a'*16,rule='合成旧规则v1')
        directory=self.root/'logs/test/T4/judgments'; directory.mkdir(parents=True)
        (directory/'旧档案.jsonl').write_text(json.dumps(old,ensure_ascii=False)+'\r\n')
        before=(directory/'旧档案.jsonl').read_bytes()
        common.persist(directory,additions=[record])
        saved=json.loads((directory/'2026-10.jsonl').read_text())
        self.assertEqual(saved['id'],'J-2026-10-23-010')
        self.assertEqual((directory/'旧档案.jsonl').read_bytes(),before)
        self.put('data/市场状态/最新.json',{'数据日期':'2026-10-22'})
        report=generate.run(self.root,'市场状态',now=self.now)
        self.assertEqual(report['data_date'],'2026-10-22')
        with self.assertRaises(ValueError): generate.run(self.root,'市场状态','2026-10-21',now=self.now)
        self.rule('market_defend')['enabled']=False
        self.rule('mainline_confirm').update(enabled=True,approved=True,approval='夹具')
        with self.assertRaises(ValueError): generate.validate_rules(self.config)

    def test_three_conditions_same_calendar_and_no_future(self):
        for kind,code,op in [('指数涨跌','sh000300','<'),('指数相对','sh000300','<'),('行业ETF相对沪深300','512880','>')]:
            record=self.record(kind,code,op)
            if kind=='指数相对': record['check']['benchmark']='sh000001'
            early=common.evaluate(self.root,record,'2026-10-23')
            self.assertIn('未到期',early['pending'])
            actual=common.evaluate(self.root,record,'2026-10-26')
            self.assertEqual(actual['target'],'2026-10-26')
            self.assertEqual(actual['status'],'证实')
            self.assertIn('实际',actual['result']['note'])
        calendar=common.TradingCalendar(self.root)
        self.assertEqual(common.evaluate(self.root,self.record(op='>'),'2026-10-26')['status'],'证伪')
        self.assertEqual(calendar.window('2026-09-30',1)[-1],dt.date(2026,10,8))
        with self.assertRaises(ValueError): calendar.window('2026-12-31',2)

    def test_missing_middle_or_target_not_rolled_forward(self):
        record=self.record('行业ETF相对沪深300','512880','>')
        self.csv('data/行情库/ETF/512880.csv',[(d,100,1) for d in self.days if d!='2026-10-23'])
        outcome=common.evaluate(self.root,record,'2026-10-30')
        self.assertIn('2026-10-23',outcome['pending']); self.assertEqual(outcome['target'],'2026-10-26')
        self.csv('data/行情库/指数/sh000300.csv',[(d,100,1) for d in self.days if d!='2026-10-26'])
        outcome=common.evaluate(self.root,self.record(),'2026-10-30')
        self.assertEqual(outcome['target'],'2026-10-26'); self.assertIn('2026-10-26',outcome['pending'])

    def test_factor_duplicate_nan_and_bad_fixed_date(self):
        record=self.record(op='>=',value=0)
        self.csv('data/行情库/指数/sh000300.csv',[(d,50 if d>='2026-10-23' else 100,2 if d>='2026-10-23' else 1) for d in self.days])
        self.assertEqual(common.evaluate(self.root,record,'2026-10-26')['actual'],0)
        record['review_date']='2026-10-27'
        with self.assertRaises(ValueError): common.evaluate(self.root,record,'2026-10-30')
        record['review_date']='2026-10-26'
        self.csv('data/行情库/指数/sh000300.csv',[(d,100,float('nan') if d=='2026-10-23' else 1) for d in self.days])
        self.assertIn('无效',common.evaluate(self.root,record,'2026-10-26')['pending'])
        self.csv('data/行情库/指数/sh000300.csv',[(d,100,1) for d in self.days]+[(self.days[0],100,1)])
        self.assertIn('重复',common.evaluate(self.root,record,'2026-10-26')['pending'])

    def test_original_seven_raw_bytes_preserved_with_append_review(self):
        path=self.root/'logs/test/T4/judgments/2026-10.jsonl'; path.parent.mkdir(parents=True)
        original=(REPO/'judgments/2026-10.jsonl').read_bytes()
        path.write_bytes(original)
        record=self.record(); common.persist(path.parent,additions=[record])
        self.assertTrue(path.read_bytes().startswith(original))
        result=review.run(self.root,'2026-10-30',path.parent,now=self.now+dt.timedelta(days=10))
        # 人工待复核条数随真实档案变化（10-22 两条已于 11-01 人工复核），按档案现状计算，不写死
        manual=sum(1 for l in original.decode('utf-8').splitlines() if l.strip()
                   for j in [json.loads(l)] if j.get('status')=='待验证' and not j.get('check') and j['review_date']<='2026-10-30')
        self.assertEqual(result['status'],'正常'); self.assertEqual(len(result['manual']),manual)
        self.assertTrue(path.read_bytes().startswith(original))
        record_after=json.loads(path.read_text().splitlines()[-1])
        self.assertEqual({k:v for k,v in record_after.items() if k not in ('result','status')},{k:v for k,v in record.items() if k not in ('result','status')})
        before=path.read_bytes(); review.run(self.root,'2026-10-30',path.parent,now=self.now+dt.timedelta(days=10))
        self.assertEqual(path.read_bytes(),before)

    def test_no_existing_result_overwrite(self):
        record=self.record(); record['result']={'date':'2026-10-23','status':'待验证','note':'已有人工备注','evidence':[]}
        directory=self.root/'logs/test/T4/judgments'; directory.mkdir(parents=True)
        path=directory/'2026-10.jsonl'; path.write_text(json.dumps(record,ensure_ascii=False)+'\n')
        before=path.read_bytes()
        result=review.run(self.root,'2026-10-30',directory,now=self.now+dt.timedelta(days=10))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(result['manual']),1)

    def test_same_month_crlf_manual_bytes_survive_append_and_review(self):
        directory=self.root/'logs/test/T4/judgments'; directory.mkdir(parents=True)
        path=directory/'2026-10.jsonl'
        original=(REPO/'judgments/2026-10.jsonl').read_bytes().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
        path.write_bytes(original)
        common.persist(directory,additions=[self.record()])
        self.assertTrue(path.read_bytes().startswith(original))
        review.run(self.root,'2026-10-30',directory,now=self.now+dt.timedelta(days=10))
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(path.read_bytes()[:len(original)].count(b'\r\n'),len(original.splitlines()))

    def test_stats_cross_product_program_and_manual(self):
        directory=self.root/'logs/test/T4/stats'; directory.mkdir(parents=True)
        record=self.record(); record['status']='证实'
        record['result']=common.evaluate(self.root,self.record(),'2026-10-26')['result']
        manual=copy.deepcopy(record); manual.update(id='J-2026-10-22-001',date='2026-10-22',auto=False,status='证伪')
        manual['result']=dict(manual['result'],status='证伪')
        (directory/'2026-10.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in (record,manual))+'\n')
        completed=subprocess.run([sys.executable,str(REPO/'scripts/check_judgments.py'),'--directory',str(directory),'--stats'],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        self.assertIn('市场状态 × 程序 × 低',completed.stdout)
        self.assertIn('市场状态 × 手写 × 低',completed.stdout)

    def test_atomic_prevalidation_and_conflicting_same_event(self):
        path=self.root/'logs/test/T4/judgments'
        good=self.record(); bad=copy.deepcopy(good); bad['confidence']='高'
        with self.assertRaises(ValueError): common.persist(path,additions=[good,bad])
        self.assertFalse(list(path.glob('*.jsonl')))
        common.persist(path,additions=[good]); before=(path/'2026-10.jsonl').read_bytes()
        conflict=copy.deepcopy(good); conflict['claim']='不允许改原文'
        with self.assertRaises(ValueError): common.persist(path,additions=[conflict])
        self.assertEqual((path/'2026-10.jsonl').read_bytes(),before)

    def test_future_temperature_does_not_change_past_trigger_and_missing_prior(self):
        rule=self.rule('sentiment_ice'); cal=common.TradingCalendar(self.root)
        first=generate.trigger_events(self.root,rule,'2026-10-22',cal,generate.temperature_map(self.root))
        self.temp('2026-10-21,30\n2026-10-22,10\n2026-10-29,99\n')
        self.assertEqual(generate.trigger_events(self.root,rule,'2026-10-22',cal,generate.temperature_map(self.root)),first)
        self.temp('2026-10-20,30\n2026-10-22,10\n')
        events,reason=generate.trigger_events(self.root,rule,'2026-10-22',cal,generate.temperature_map(self.root))
        self.assertEqual(events,[]); self.assertIn('前一交易日',reason)

    def test_schema_rejects_invalid_types_and_path_traversal(self):
        record=self.record()
        for key,value in [('auto','true'),('confidence','高'),('module',[]),('check',[]),('id',{}),('auto_key','xxx')]:
            bad=copy.deepcopy(record); bad[key]=value
            self.assertTrue(checker.check_record(bad,'坏夹具'),key)
        for field,value in [('type',[]),('op',[]),('value',float('nan')),('to_trading_days',True)]:
            bad=copy.deepcopy(record); bad['check'][field]=value
            self.assertTrue(checker.check_record(bad,'坏条件'),field)
        record['evidence'][0]['source']='data/../judgments/2026-10.jsonl'
        self.assertTrue(checker.check_record(record,'路径'))


    def test_t4b_start_drawdown_is_neither_endpoint_nor_rolling_peak(self):
        record=self.record('指数最大回撤',op='>=',value=.05)
        # 100→120→96：起点回撤4%，滚动高点回撤20%；端点涨跌-4%。
        self.csv('data/行情库/指数/sh000300.csv',[(d, { '2026-10-22':100,'2026-10-23':120,'2026-10-26':96}.get(d,100),1) for d in self.days])
        result=common.evaluate(self.root,record,'2026-10-26')
        self.assertEqual(result['status'],'证伪'); self.assertAlmostEqual(result['actual'],.04)
        self.assertIn('起点收盘',result['result']['evidence'][0]['text'])
        self.csv('data/行情库/指数/sh000300.csv',[(d,100 if d=='2026-10-22' else 120,1) for d in self.days])
        self.assertEqual(common.evaluate(self.root,record,'2026-10-26')['actual'],0)

    def test_t4b_drawdown_factor_fixed_window_and_bad_threshold(self):
        record=self.record('指数最大回撤',op='>=',value=.05)
        self.csv('data/行情库/指数/sh000300.csv',[(d,50 if d=='2026-10-23' else 100,2 if d=='2026-10-23' else 1) for d in self.days])
        self.assertEqual(common.evaluate(self.root,record,'2026-10-26')['actual'],0)
        self.assertIn('未到期',common.evaluate(self.root,record,'2026-10-23')['pending'])
        self.csv('data/行情库/指数/sh000300.csv',[(d,100,1) for d in self.days if d!='2026-10-23'])
        self.assertIn('缺日',common.evaluate(self.root,record,'2026-10-29')['pending'])
        for value in (-.01,1.01):
            bad=copy.deepcopy(record); bad['check']['value']=value
            self.assertTrue(checker.check_record(bad,'错误回撤'))

    def test_t4b_enter_requires_previous_trading_day_non_defend(self):
        rule=next(r for r in json.loads((REPO/'system/自动判断规则.json').read_text())['rules'] if r['id']=='market_defend_risk')
        calendar=common.TradingCalendar(self.root)
        event,reason=generate.trigger_events(self.root,rule,'2026-10-22',calendar,{})
        self.assertEqual(event,[]); self.assertIn('缺失',reason)
        self.market('2026-10-21','守')
        self.assertEqual(generate.trigger_events(self.root,rule,'2026-10-22',calendar,{})[0],[])
        self.market('2026-10-21','中性')
        event,reason=generate.trigger_events(self.root,rule,'2026-10-22',calendar,{})
        self.assertIsNone(reason); self.assertEqual(len(event),1)
        self.assertEqual(event[0]['type'],'指数最大回撤')
        self.assertIn('2026-10-21为中性',event[0]['text'])
        self.put('data/市场状态/2026-10/2026-10-21.json',{'数据日期':'2026-10-20','市场':{'结论':'攻'}})
        self.assertIn('不可确认',generate.trigger_events(self.root,rule,'2026-10-22',calendar,{})[1])

    def test_t4b_approval_policy_and_legacy_compatibility(self):
        config=json.loads((REPO/'system/自动判断规则.json').read_text())
        generate.validate_rules(config)
        enabled=[r for r in config['rules'] if generate.active(r,False,False)[0]]
        self.assertEqual({r['id'] for r in enabled},{'market_defend_risk','flow_proxy_top5'})
        self.assertTrue(all(r['approved']=='Claude 审核 2026-10-29，依据 PR #6 评论' for r in enabled))
        for r in config['rules']:
            if r not in enabled: self.assertFalse(generate.active(r,True,True)[0])
        self.assertFalse(generate.approved({'approved':True}))
        self.assertTrue(generate.approved({'approved':True,'approval':'旧格式批准'}))
        bad=copy.deepcopy(config); bad['rules'][2]['approved']=' '
        with self.assertRaises(ValueError): generate.validate_rules(bad)

    def test_t4b_full_history_pending_denominators_and_isolation(self):
        import auto_judgments_full_history as history
        # 延长真实交易日历，使前两个进入事件有20日窗口，尾部事件保持待到期。
        days=[(dt.date(2026,9,1)+dt.timedelta(days=i)).isoformat() for i in range(59)
              if (dt.date(2026,9,1)+dt.timedelta(days=i)).weekday()<5 and (dt.date(2026,9,1)+dt.timedelta(days=i)).isoformat() not in {'2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07'}]
        self.csv('data/行情库/指数/sh000300.csv',[(d,100-i,1) for i,d in enumerate(days)])
        path=self.root/'logs/市场状态_规则检验_逐日.csv'; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('date,结论,5日涨跌,20日最大回撤\n'+''.join(f"{d},{state},{ret},{dd}\n" for d,state,ret,dd in [
            (days[0],'守',1,-6),(days[1],'攻',1,-1),(days[2],'守',-1,-8),(days[3],'守','',''),(days[-2],'攻',1,-2),(days[-1],'守','','')]))
        self.temp(''.join(f'{d},{v}\n' for d,v in zip(days[:5],[30,10,10,90,90])))
        guard=self.root/'judgments/2026-10.jsonl'; guard.parent.mkdir(); guard.write_text('原文保护\n')
        report=history.full_history(self.root)
        defend=next(s for s in report['cached_daily'] if s['name']=='守之后5日上涨')
        self.assertEqual((defend['samples'],defend['complete'],defend['pending'],defend['hit']),(4,2,2,1))
        self.assertEqual(defend['review_share'],.25); self.assertEqual(defend['hit_rate'],.5)
        self.assertEqual([e['date'] for e in report['entry_events']],[days[2],days[-1]])
        self.assertEqual(report['entry_previous_unknown'],[days[0]])
        self.assertEqual(report['entry_risk']['pending'],1)
        self.assertEqual(len(report['sentiment_events']['冰点']),1)
        self.assertEqual(len(report['sentiment_events']['过热']),1)
        self.assertEqual(guard.read_text(),'原文保护\n')
        with self.assertRaises(ValueError): history.full_history(self.root,self.root/'judgments')
        path.write_text(path.read_text()+f'{days[0]},攻,1,-1\n')
        with self.assertRaises(ValueError): history.full_history(self.root)


if __name__=='__main__': unittest.main()
