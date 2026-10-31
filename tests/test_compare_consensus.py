"""预定来源门槛、年度标记与零/负EPS边界。"""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import consensus_common as c


class ConsensusComparisonTests(unittest.TestCase):
    def raw(self):return {'SECURITY_CODE':'600000','SECURITY_NAME_ABBR':'合成','YEAR1':2025,'YEAR_MARK1':'A','EPS1':99,'YEAR2':2026,'YEAR_MARK2':'E','EPS2':1.02,'YEAR3':2027,'YEAR_MARK3':'E','EPS3':-2.04,'RATING_ORG_NUM':7}
    def test_year_and_estimate_mark_both_required(self):
        raw=self.raw();r=c.em_record(raw,2026);self.assertEqual(r['eps'],{'2026':1.02,'2027':-2.04});self.assertTrue(r['年度映射有效'])
        raw['YEAR_MARK2']='A';r=c.em_record(raw,2026);self.assertIsNone(r['eps']['2026']);self.assertFalse(r['年度映射有效'])
    def test_two_percent_both_years_and_counts_independent(self):
        r=c.compare_row(c.em_record(self.raw(),2026),{'eps':{'2026':1,'2027':-2},'机构数':{'2026':5,'2027':6}},2026)
        self.assertTrue(r['两年EPS均通过']);self.assertFalse(any(r['机构数逐年相同'].values()))
        raw=self.raw();raw['EPS3']=-2.05;r=c.compare_row(c.em_record(raw,2026),{'eps':{'2026':1,'2027':-2}},2026);self.assertFalse(r['两年EPS均通过'])
    def test_zero_missing_and_negative_denominator(self):
        self.assertEqual(c.relative_difference(0,0),0);self.assertIsNone(c.relative_difference(1,0))
        self.assertAlmostEqual(c.relative_difference(-1.02,-1),.02);self.assertIsNone(c.relative_difference(None,1))
    def fixture(self,passed=45):
        return [{'code':f'{i:06d}','两年EPS均通过':i<passed,'年度映射一致':True,'机构数逐年相同':{'2026':False,'2027':False}} for i in range(50)]
    def test_exact_45_of_50_and_full_year_mapping_gate(self):
        self.assertEqual(c.decide(self.fixture())['主来源'],'东财批量');self.assertEqual(c.decide(self.fixture(44))['主来源'],'同花顺')
        rows=self.fixture(50);rows[-1]['年度映射一致']=False;self.assertEqual(c.decide(rows)['主来源'],'同花顺')
    def test_cannot_drop_failed_or_duplicate_sample(self):
        for rows in (self.fixture()[:-1],self.fixture()[:49]+[self.fixture()[0]]):
            with self.assertRaises(ValueError):c.decide(rows)
    def test_membership_is_existing_14_industry_scope(self):
        rows,sha=c.industry_members(Path(__file__).resolve().parents[1]);self.assertGreater(len(rows),2000)
        self.assertEqual(len({r['申万一级代码'] for r in rows.values()}),14);self.assertEqual(len(sha),64)


if __name__=='__main__':unittest.main()
