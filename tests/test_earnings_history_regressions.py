"""阶段1独立审核反例：整份拒绝、真实探测闭环、成功高水位及回滚。"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import test_earnings_history as fixtures
FakeClient=fixtures.FakeClient
page=fixtures.page
import earnings_history as eh
import earnings_history_publish as publisher
import earnings_history_transaction as transaction


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.EarningsTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.base=self.root/'data/财报/全市场单季'

    def artifact(self):
        path=self.fixture.artifact();payload=json.loads(path.read_text());shutil.rmtree(self.base)
        return path,payload

    def check_rejected(self,payload,path):
        path.write_text(json.dumps(payload,ensure_ascii=False))
        with self.assertRaises(ValueError):publisher.publish(self.root,path,'123','a'*40,check_ancestry=False)
        self.assertFalse(self.base.exists())
        self.assertFalse((self.root/'logs/test/T13b/发布_123.json').exists())

    def test_incomplete_or_wrongly_typed_artifact_never_promotes_any_file(self):
        path,original=self.artifact()
        mutations={
            'missing_stock':lambda p:p['单季记录'].pop('000002'),
            'missing_time':lambda p:p['报告']['结果'][1].pop('采集时间'),
            'bad_time':lambda p:p['报告'].__setitem__('生成时间','9999-invalid'),
            'missing_revenue':lambda p:p['单季记录']['000002'][0].pop('营业总收入'),
            'dict_revenue':lambda p:p['单季记录']['000002'][0].__setitem__('营业总收入',{'注入':'值'}),
            'infinite_revenue':lambda p:p['单季记录']['000002'][0].__setitem__('营业总收入','inf'),
            'unknown_amount':lambda p:p['单季记录']['000002'][0].__setitem__('营业总收入','不明值'),
            'duplicate_status':lambda p:p['报告']['结果'].append(copy.deepcopy(p['报告']['结果'][0])),
            'duplicate_period':lambda p:p['单季记录']['000002'].append(copy.deepcopy(p['单季记录']['000002'][0])),
            'wrong_count':lambda p:p['报告']['结果'][1].__setitem__('行数',2),
            'mismatched_record_time':lambda p:p['报告']['结果'][1].__setitem__('采集时间','2026-10-01T00:00:00+08:00'),
            'missing_raw_key':lambda p:p['单季记录']['000001'][0].pop('扣非净利润'),
        }
        for name,mutate in mutations.items():
            with self.subTest(case=name):
                payload=copy.deepcopy(original);mutate(payload);self.check_rejected(payload,path)

    def probe(self):
        eh.collect(self.root,mode='probe',codes=['000001','600519','000063'],client=FakeClient({c:page() for c in ['000001','600519','000063']}))
        path=next((self.root/'logs/test/T13b/产物').glob('*/artifact.json'))
        payload=json.loads(path.read_text());payload['报告'].update({'来源分支':'gpt/T13b-earnings','来源运行':'123','来源SHA':'a'*40})
        path.write_text(json.dumps(payload,ensure_ascii=False));return path,payload

    def test_three_unique_probes_need_matching_saved_html_and_reparse(self):
        path,payload=self.probe();publisher.publish(self.root,path,'123','a'*40,check_ancestry=False)
        eh.probe_ready(self.root)
        self.assertFalse(self.base.exists())
        first=payload['报告']['结果'][0];raw=self.root/first['响应文件'];original=raw.read_text();raw.unlink()
        with self.assertRaisesRegex(ValueError,'3只不同股票'):eh.probe_ready(self.root)
        raw.write_text(original+'改动')
        with self.assertRaisesRegex(ValueError,'3只不同股票'):eh.probe_ready(self.root)
        raw.write_text(original);eh.probe_ready(self.root)

    def test_duplicate_or_missing_probe_payload_cannot_unlock(self):
        path,payload=self.probe();record=copy.deepcopy(payload['报告']['结果'][0])
        payload['报告']['结果']=[record,record,record];payload['单季记录']={};payload['探测响应']={}
        self.check_rejected(payload,path)
        evidence={'来源运行':'123','来源SHA':'a'*40,'采集报告':payload['报告'],'探测单季记录':{}}
        (self.root/'logs/test/T13b/发布_123.json').write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError,'3只不同股票'):eh.probe_ready(self.root)

    def test_real_html_crlf_survives_publish_and_probe_gate(self):
        codes=['000001','600519','000063']
        text=page().replace('><','>\r\n<')+'\r\n'
        eh.collect(self.root,mode='probe',codes=codes,client=FakeClient({c:text for c in codes}))
        path=next((self.root/'logs/test/T13b/产物').glob('*/artifact.json'))
        payload=json.loads(path.read_text())
        payload['报告'].update({'来源分支':'gpt/T13b-earnings','来源运行':'123','来源SHA':'a'*40})
        path.write_text(json.dumps(payload,ensure_ascii=False))
        publisher.publish(self.root,path,'123','a'*40,check_ancestry=False)
        eh.probe_ready(self.root)
        for record in payload['报告']['结果']:
            self.assertEqual((self.root/record['响应文件']).read_bytes(),text.encode())
        # 语义不变的换行篡改也必须被原始响应SHA拒绝。
        raw=self.root/payload['报告']['结果'][0]['响应文件']
        raw.write_bytes(raw.read_bytes().replace(b'\r\n',b'\n'))
        with self.assertRaisesRegex(ValueError,'3只不同股票'):eh.probe_ready(self.root)

    def test_probe_cannot_write_request_or_mismatch_html_manifest(self):
        path,original=self.probe()
        payload=copy.deepcopy(original);payload['报告']['结果'][0]['响应文件']='logs/test/T13b/发布请求.json'
        self.check_rejected(payload,path)
        self.assertFalse((self.root/'logs/test/T13b/发布请求.json').exists())
        payload=copy.deepcopy(original);code=payload['报告']['结果'][0]['代码']
        changed=page(kf=['1亿','9亿']);payload['探测响应'][code]=changed
        payload['报告']['结果'][0]['响应SHA256']=hashlib.sha256(changed.encode()).hexdigest()
        self.check_rejected(payload,path)

    def test_unchanged_and_empty_success_advance_watermark_old_payload_rejected(self):
        path,original=self.artifact()
        def commit(run,day,value='1亿',empty=False):
            payload=copy.deepcopy(original);stamp='2026-10-'+day+'T12:00:00+08:00'
            payload['报告'].update({'来源运行':run,'生成时间':stamp})
            for record in payload['报告']['结果']:
                record['采集时间']=stamp;code=record['代码']
                if empty:
                    payload['单季记录'][code]=[];record.update({'状态':'无2013起历史','行数':0,'起点':'','终点':'','净利润回退行数':0})
                else:payload['单季记录'][code][0]['扣非净利润']=value
                record['载荷SHA256']=eh.rows_digest(payload['单季记录'][code])
            path.write_text(json.dumps(payload,ensure_ascii=False))
            return publisher.publish(self.root,path,run,'a'*40,check_ancestry=False)
        commit('1','01');before=(self.base/'000001.csv').read_bytes()
        r=commit('3','03');self.assertEqual(r['追加行']['000001'],0)
        self.assertEqual((self.base/'000001.csv').read_bytes(),before)
        files={p:p.read_bytes() for p in self.base.iterdir()}
        with self.assertRaisesRegex(ValueError,'高水位'):commit('2','02','9亿')
        self.assertTrue(all(p.read_bytes()==b for p,b in files.items()))
        commit('5','05',empty=True)
        with self.assertRaisesRegex(ValueError,'高水位'):commit('4','04','9亿')
        self.assertFalse((self.root/'logs/test/T13b/发布_2.json').exists())

    def test_stage_failure_and_commit_failure_restore_every_file(self):
        path,_=self.artifact();real=publisher.append_rows;calls=[0]
        def fail_second(*args,**kwargs):
            calls[0]+=1
            if calls[0]==2:raise OSError('模拟暂存失败')
            return real(*args,**kwargs)
        with patch.object(publisher,'append_rows',side_effect=fail_second):
            with self.assertRaises(OSError):publisher.publish(self.root,path,'123','a'*40,check_ancestry=False)
        self.assertFalse(self.base.exists())
        replace=transaction.os.replace;calls[0]=0
        def fail_promotion(source,target):
            if str(source).endswith('.new'):
                calls[0]+=1
                if calls[0]==2:raise OSError('模拟第二文件晋升失败')
            return replace(source,target)
        with patch.object(transaction.os,'replace',side_effect=fail_promotion):
            with self.assertRaises(OSError):publisher.publish(self.root,path,'123','a'*40,check_ancestry=False)
        self.assertFalse(self.base.exists())
        self.assertFalse((self.root/'logs/test/T13b/发布_123.json').exists())
        existing=self.root/'existing.txt';existing.write_bytes(b'old')
        calls[0]=0
        with patch.object(transaction.os,'replace',side_effect=fail_promotion):
            with self.assertRaises(OSError):transaction.commit_files(self.root,{existing:b'new',self.root/'new.txt':b'new'})
        self.assertEqual(existing.read_bytes(),b'old');self.assertFalse((self.root/'new.txt').exists())

    def test_workflow_never_saves_failed_promotion(self):
        text=(ROOT/'.github/workflows/test-t13b-publish-earnings-history.yml').read_text()
        self.assertIn("if: always() && steps.promote.outcome == 'success'",text)
        self.assertIn('SIGNALS_OUTCOME: ${{ steps.signals.outcome }}',text)


if __name__=='__main__':unittest.main()
