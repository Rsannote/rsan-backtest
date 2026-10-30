"""T13b 离线固定页面：解析、截止日、断点、错误和只追加验收。"""
import csv
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import earnings_history as earnings
from earnings_history_signals import append_plan,preserve_unchanged_notes
from flow_source import append_rows
from earnings_history_publish import publish


def page(dates=None, kf=None):
    dates = dates if dates is not None else ['2012-12-31', '2013-03-31']
    n = len(dates)
    obj = {'title': [['报告期', '日期'], ['扣非净利润', '元'], '净利润', ['营业总收入', '元']],
           'simple': [dates, kf if kf is not None else ['1亿'] * n, ['2亿'] * n, ['3亿'] * n]}
    return '<html><p id="main">' + json.dumps(obj, ensure_ascii=False) + '</p></html>'


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, code):
        self.calls.append(code)
        result = self.responses[code]
        if isinstance(result, Exception):
            raise result
        return result


class EarningsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/v3_select.py', self.root / 'scripts/v3_select.py')
        shutil.copyfile(ROOT / 'scripts/earnings_history.py', self.root / 'scripts/earnings_history.py')
        p = self.root / 'data/v3/申万成分.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'801010': {'date': '2026-10-21', 'rows': [{'证券代码': '000001'}, {'证券代码': '000002'}]}}))
        self.num, _, _ = earnings.load_v3(self.root)

    def test_official_matrix_transpose_keeps_raw_units_and_fallback(self):
        rows = earnings.parse_page(page(kf=['1亿', '--']), '000001', self.num)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['报告期'], '2013-03-31')
        self.assertEqual(rows[0]['扣非净利润'], '--')
        self.assertEqual(rows[0]['利润口径'], '净利润回退')
        self.assertEqual(rows[0]['净利润'], '2亿')
        self.assertEqual(rows[0]['法定截止'], '2013-04-30')
        self.assertEqual(rows[0]['可见时刻'], '2013-05-01T00:00:00+08:00')

    def test_all_four_legal_deadlines_and_original_v3_exclusive_day(self):
        _, q_signal, _ = earnings.load_v3(self.root)
        for period, expected in [('2025-03-31', '2025-04-30'), ('2025-06-30', '2025-08-31'), ('2025-09-30', '2025-10-31'), ('2025-12-31', '2026-04-30')]:
            with self.subTest(period=period):
                self.assertEqual(earnings.deadline(period).isoformat(), expected)
                rows = [{'报告期': period, '扣非净利润': '1', '营业总收入': '1'}]
                self.assertIsNone(q_signal(rows, dt.date.fromisoformat(expected)))
                self.assertEqual(q_signal(rows, dt.date.fromisoformat(expected) + dt.timedelta(days=1))['q'], period)

    def test_empty_old_history_distinct_from_http_or_schema_error(self):
        self.assertEqual(earnings.parse_page(page(dates=[]), '000001', self.num), [])
        self.assertEqual(earnings.parse_page(page(dates=['2012-03-31']), '000001', self.num), [])
        for html in ['<html>403 forbidden</html>', '<p id="main">{}</p>', page().replace('2013-03-31', '2013-03-30'), page().replace('净利润', '未知')]:
            with self.subTest(html=html[:50]), self.assertRaises((ValueError, KeyError)):
                earnings.parse_page(html, '000001', self.num)

    def test_source_false_is_explicit_missing_but_numeric_zero_is_not(self):
        rows=earnings.parse_page(page(dates=['2013-03-31','2013-06-30'],kf=[False,0]),'000001',self.num)
        self.assertEqual(rows[0]['扣非净利润'],'False')
        self.assertEqual(rows[0]['利润口径'],'净利润回退')
        self.assertNotEqual(self.num(rows[0]['扣非净利润']),self.num(rows[0]['扣非净利润']))
        self.assertEqual(rows[1]['扣非净利润'],'0')
        self.assertEqual(rows[1]['利润口径'],'扣非净利润')
        for value in (True,{},[]):
            with self.subTest(value=value),self.assertRaises(ValueError):
                earnings.parse_page(page(dates=['2013-03-31'],kf=[value]),'000001',self.num)

    def test_batch_resume_retries_only_failure_and_is_append_only(self):
        client = FakeClient({'000001': page(), '000002': requests.HTTPError('503')})
        first = earnings.collect(self.root, mode='batch', client=client)
        self.assertEqual(first['失败数'], 1)
        target = self.root / 'data/财报/全市场单季/000001.csv'
        before = target.read_bytes()
        retry = FakeClient({'000002': page()})
        second = earnings.collect(self.root, mode='batch', client=retry)
        self.assertEqual(second['断点跳过'], 1)
        self.assertEqual(retry.calls, ['000002'])
        self.assertEqual(target.read_bytes(), before)
        changed = FakeClient({'000001': page(kf=['1亿', '9亿']), '000002': page()})
        earnings.collect(self.root, mode='batch', client=changed, refresh=True)
        self.assertTrue(target.read_bytes().startswith(before))
        with target.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['扣非净利润'], '9亿')
        before = target.read_bytes()
        earnings.collect(self.root, mode='batch', client=changed, refresh=True)
        self.assertEqual(target.read_bytes(), before)

    def test_probe_does_not_write_official_data(self):
        earnings.collect(self.root, mode='probe', codes=['000001'], client=FakeClient({'000001': page()}))
        self.assertFalse((self.root / 'data/财报/全市场单季').exists())
        self.assertTrue(list((self.root / 'logs/test/T13b/抓取响应').glob('*/*.html')))

    def test_budget_and_batch_size_and_no_local_network(self):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}):
            with self.assertRaisesRegex(RuntimeError, 'Actions'):
                earnings.Client(1)
        for budget in [0, 3001]:
            with self.assertRaises(ValueError):
                earnings.collect(self.root, budget=budget, client=FakeClient({}))
        client = FakeClient({'000001': page(), '000002': page()})
        result = earnings.collect(self.root, mode='batch', max_stocks=1, client=client)
        self.assertEqual(result['本批尝试'], 1)
        self.assertEqual(client.calls, ['000001'])
        with patch('earnings_history.time.monotonic', side_effect=[0, 5000]):
            result = earnings.collect(self.root, mode='batch', refresh=True, client=client)
        self.assertEqual(result['本批尝试'], 0)

    def artifact(self):
        earnings.collect(self.root,mode='batch',client=FakeClient({'000001':page(),'000002':page()}))
        artifact=next((self.root/'logs/test/T13b/产物').glob('*/artifact.json'))
        payload=json.loads(artifact.read_text());payload['报告'].update({'来源分支':'gpt/T13b-earnings','来源运行':'123','来源SHA':'a'*40})
        artifact.write_text(json.dumps(payload,ensure_ascii=False));return artifact

    def test_publish_validates_origin_and_is_idempotent(self):
        artifact=self.artifact();base=self.root/'data/财报/全市场单季'
        shutil.rmtree(base)
        first=publish(self.root,artifact,'123','a'*40,check_ancestry=False)
        self.assertEqual(sum(first['追加行'].values()),2)
        before={p:p.read_bytes() for p in base.iterdir()}
        second=publish(self.root,artifact,'123','a'*40,check_ancestry=False)
        self.assertEqual(sum(second['追加行'].values()),0)
        self.assertTrue(all(p.read_bytes()==v for p,v in before.items()))
        with self.assertRaisesRegex(ValueError,'分支/运行/SHA'):publish(self.root,artifact,'456','a'*40,check_ancestry=False)
        self.assertTrue(all(p.read_bytes()==v for p,v in before.items()))

    def test_publish_rejects_bad_legal_visibility_before_any_write(self):
        artifact=self.artifact();base=self.root/'data/财报/全市场单季';shutil.rmtree(base)
        payload=json.loads(artifact.read_text());payload['单季记录']['000002'][0]['可见时刻']='2013-04-30T00:00:00+08:00'
        artifact.write_text(json.dumps(payload,ensure_ascii=False))
        with self.assertRaisesRegex(ValueError,'可见边界'):publish(self.root,artifact,'123','a'*40,check_ancestry=False)
        self.assertFalse(base.exists())

    def test_batch_requires_published_probe_before_network(self):
        with self.assertRaisesRegex(ValueError,'3只不同股票'):earnings.probe_ready(self.root)

    def test_pure_note_change_keeps_existing_version(self):
        import pandas as pd
        path=self.root/'signals.csv'
        fields=['date','code','value','使用时点说明','生成时间','修订原因']
        rows=[{'date':'2026-01-01','code':'1','value':1,'使用时点说明':'旧注记'}]
        append_rows(path,fields,rows,['date','code'],'时间')
        frame=pd.DataFrame([dict(rows[0],使用时点说明='新引用')])
        result=preserve_unchanged_notes(path,frame)
        self.assertEqual(result.iloc[0]['使用时点说明'],'旧注记')
        frame.loc[0,'value']=2
        self.assertEqual(preserve_unchanged_notes(path,frame).iloc[0]['使用时点说明'],'新引用')

    def test_append_plan_matches_actual_bytes_and_zero_on_repeat(self):
        p = self.root / 'signals.csv'
        fields = ['date', 'code', 'value', '生成时间', '修订原因']
        rows = [{'date': '2026-01-01', 'code': '中', 'value': 1/3}]
        plan = append_plan(p, fields, rows, '北京时间', 'T13b-v2')
        append_rows(p, fields, rows, ['date', 'code'], '北京时间', revision_reason='T13b-v2')
        self.assertEqual(p.stat().st_size, plan['预计总字节'])
        self.assertEqual(append_plan(p, fields, rows, '晚一点', 'T13b-v2')['追加字节'], 0)
        before = p.read_bytes()
        rows[0]['value'] = None
        plan = append_plan(p, fields, rows, '晚一点', 'T13b-v2')
        append_rows(p, fields, rows, ['date', 'code'], '晚一点', revision_reason='T13b-v2')
        self.assertEqual(p.stat().st_size, plan['预计总字节'])
        self.assertTrue(p.read_bytes().startswith(before))


if __name__ == '__main__':
    unittest.main()
