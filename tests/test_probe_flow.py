"""合成响应验证，禁止把成交量或价格数组错认成交额。"""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import probe_flow as p


class FlowProbeTests(unittest.TestCase):
    def test_official_amount_and_identity(self):
        row={'swindexcode':'801010','bargaindate':'2013-01-04T00:00:00','closeindex':100,'bargainsum':123,'bargainamount':7}
        r=p.parse_history(json.dumps({'data':[row]}),'申万官网','801010')
        self.assertTrue(r['全部日期含有效成交额']);self.assertEqual(r['样例'][0]['amount'],123)
        with self.assertRaises(ValueError):p.parse_history(json.dumps({'data':[row]}),'申万官网','801020')
    def test_tencent_price_only_not_amount(self):
        text=json.dumps({'code':0,'data':{'sw801010':{'day':[['2026-10-29',1,2,3,1,100,{'turnover':9}]]}}})
        r=p.parse_history(text,'腾讯sw前缀','sw801010');self.assertFalse(r['全部日期含有效成交额'])
        self.assertIsNone(r['成交额原字段'])
    def test_sina_volume_not_amount_and_empty(self):
        r=p.parse_history('[{"day":"2026-10-29","close":2,"volume":100}]','新浪sw前缀','sw801010')
        self.assertFalse(r['全部日期含有效成交额'])
        for s in ('[]','null'):
            with self.assertRaises((ValueError,TypeError)):p.parse_history(s,'新浪sw前缀','sw801010')
    def test_invalid_dates_duplicates_and_nonfinite(self):
        row={'swindexcode':'801010','bargaindate':'2026-10-29','closeindex':1,'bargainsum':2}
        for rows in ([row,row],[dict(row,closeindex=float('nan'))],[dict(row,bargaindate='bad')]):
            with self.assertRaises(ValueError):p.parse_history(json.dumps({'data':rows}),'申万官网','801010')
    def test_listing_must_be_complete(self):
        row={'swindexcode':'801010','swindexname':'合成行业'}
        self.assertEqual(p.industry_list(json.dumps({'data':{'count':1,'results':[row]}})),{'801010':'合成行业'})
        with self.assertRaises(ValueError):p.industry_list(json.dumps({'data':{'count':2,'results':[row]}}))


if __name__=='__main__':unittest.main()
