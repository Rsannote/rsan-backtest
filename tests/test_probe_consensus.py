"""一致预期解析合成夹具；不联网、不产生正式快照。"""
import json
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import probe_consensus as probe


class ConsensusTests(unittest.TestCase):
    def page(self,year=2026,label='预测年报每股收益',mean='均值'):
        return label+'<table><tr><th>年度</th><th>预测机构数</th><th>'+mean+'</th></tr><tr><td>'+str(year)+'</td><td>12</td><td>-1.25</td></tr><tr><td>'+str(year+1)+'</td><td>10</td><td>2.5</td></tr></table>'
    def test_exact_eps_years_mean_and_counts(self):
        r=probe.parse_ths(self.page(),2026)
        self.assertEqual(r['eps'],{'2026':-1.25,'2027':2.5});self.assertEqual(r['机构数']['2026'],12)
        self.assertIsNone(r['来源更新时间'])
    def test_profit_history_and_single_year_are_not_eps(self):
        for text in (self.page(label='预测年报净利润'),self.page(year=2024),self.page().replace('2027','2025')):
            with self.assertRaises(ValueError):probe.parse_ths(text,2026)
    def test_no_coverage_explicit_and_missing_not_zero(self):
        self.assertEqual(probe.parse_ths('本年度暂无机构做出业绩预测',2026)['eps'],{})
        self.assertIsNone(probe.numeric('--'));self.assertEqual(probe.numeric('0'),0)
        for value in ('inf','-inf'):
            with self.assertRaises(ValueError):probe.numeric(value)
    def test_secondary_year_mapping_and_report_count_not_org_count(self):
        row={'SECURITY_CODE':'600000','YEAR1':2025,'EPS1':1,'YEAR2':2026,'EPS2':2,'YEAR3':2027,'EPS3':3,'RATING_ORG_NUM':99}
        r=probe.parse_secondary(json.dumps({'success':True,'result':{'data':[row],'count':2000,'pages':4}}),2026)
        self.assertEqual(r['样例'][0]['eps'],{'2026':2,'2027':3});self.assertIsNone(r['样例'][0]['机构数'])
        self.assertFalse(r['允许作主来源']);self.assertFalse(r['完整遍历全市场']);self.assertEqual(r['两年EPS可解析行数'],1)
    def test_invalid_response_and_network_guard(self):
        with self.assertRaises(ValueError):probe.parse_secondary('{"success":false}',2026)
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}):
            with self.assertRaises(RuntimeError):probe.HTTP(0).get('https://example.invalid')
    def test_sample_denominator_from_stock_universe(self):
        rows,total=probe.sample_codes(Path(__file__).resolve().parents[1],8)
        self.assertEqual(len(rows),8);self.assertEqual(len({r['code'] for r in rows}),8);self.assertGreater(total,len(rows))


if __name__=='__main__':unittest.main()
