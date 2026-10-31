"""T13c严格IPO时点、未知分母、来源修订与派生侧表事务边界。"""
import copy
import csv
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import earnings_coverage as cov
import earnings_coverage_batch as batch
from earnings_universe import THS_COMPANY, SINA_COMPANY
from mainline_signal_store import necessary_time, write_revisions
try:
    from tests import test_mainline_signals as fixtures
except ImportError:  # 兼容直接执行 tests/test_earnings_coverage.py
    import test_mainline_signals as fixtures


def listing(code, date, *, at='2026-10-30T12:00:00+08:00', status='成功'):
    return {'代码': code, '状态': status, '上市日期': date, '原始日期': date,
            '来源字段': '上市日期', '来源': THS_COMPANY.format(code=code),
            '实现版本': cov.LISTING_VERSION, '响应SHA256': 'a' * 64, '采集时间': at}


class ListedDenominatorTests(unittest.TestCase):
    def test_ipo_day_before_equal_and_after_quarter_visibility_and_unknown(self):
        records = {c: listing(c, day) for c, day in [('000001', '2026-08-31'), ('000002', '2026-09-01'), ('000003', '2026-09-02')]}
        result = cov.listed_members(['000001', '000002', '000003', '000004'], '2026-06-30', records)
        self.assertEqual(result['已上市代码'], ['000001'])
        self.assertEqual(result['未上市代码'], ['000002', '000003'])
        self.assertEqual(result['未知代码'], ['000004'])
        self.assertEqual(result['当前成分总量'], result['已上市分母'] + result['未上市家数'] + result['上市日期未知家数'])

    def test_after_quarter_visibility_ipo_does_not_expand_old_quarter_denominator(self):
        records = {'000001': listing('000001', '2026-09-02')}
        self.assertEqual(cov.listed_members(['000001'], '2026-06-30', records)['已上市分母'], 0)
        self.assertEqual(cov.listed_members(['000001'], '2026-09-30', records)['已上市分母'], 1)

    def test_unknown_records_are_not_inferred_from_first_financial_or_index_date(self):
        fixture = fixtures.SignalsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        code = fixture.quarter_fixture()
        path = fixture.root / 'data/财报/上市日期.jsonl'
        path.unlink()
        fixture.json('data/行情库/股票/000001.json', {'首日': '2005-01-01'})
        source = fixture.source()
        source.members[code]['rows'][0]['计入日期'] = '2001-01-01'
        result = source.quarter('2026-09-01', code)
        self.assertEqual(result['业绩_可评估家数'], 0)
        self.assertIsNone(result['业绩_季报覆盖率'])
        self.assertEqual(source.quarter_details[('2026-09-01', code)]['上市日期未知家数'], 2)

    def test_pre_ipo_financial_history_does_not_make_stock_eligible(self):
        fixture = fixtures.SignalsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        code = fixture.quarter_fixture()
        path = fixture.root / 'data/财报/上市日期.jsonl'
        path.write_text(json.dumps(listing('000001', '2026-09-02')) + '\n')
        source = fixture.source()
        result = source.quarter('2026-10-30', code)
        self.assertEqual(result['业绩_可评估家数'], 0)
        self.assertIsNone(result['业绩_季报覆盖率'])
        detail = source.quarter_details[('2026-10-30', code)]
        self.assertEqual((detail['已上市分母'], detail['未上市家数'], detail['上市日期未知家数']), (0, 1, 1))

    def test_known_ipo_denominator_restores_coverage_without_relaxing_minimum_ten(self):
        fixture = fixtures.SignalsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        code = fixture.quarter_fixture()
        template = fixtures.src.csv_rows(fixture.root / 'data/财报/单季/000001.csv')
        records = [listing(str(i).zfill(6), '2010-01-01' if i < 10 else '2026-09-02') for i in range(15)]
        (fixture.root / 'data/财报/上市日期.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
        source = fixture.source()
        source.members[code] = {'date': '2026-10-21', 'rows': [{'证券代码': str(i).zfill(6)} for i in range(20)]}
        source.quarters = {str(i).zfill(6): template for i in range(15)}
        result = source.quarter('2026-09-01', code)
        self.assertEqual(result['业绩_成分家数'], 20)
        self.assertEqual(result['业绩_可评估家数'], 10)
        self.assertEqual(result['业绩_季报覆盖率'], 1)
        self.assertEqual(result['业绩_利润加速占比'], 1)
        detail = source.quarter_details[('2026-09-01', code)]
        self.assertEqual((detail['已上市分母'], detail['未上市家数'], detail['上市日期未知家数']), (10, 5, 5))
        source.quarter_cache.clear();source.quarters.pop('000009');source.stock_quarter_cache.clear()
        self.assertIsNone(source.quarter('2026-09-01', code)['业绩_利润加速占比'])

    def test_late_announcement_still_limits_numerator_with_listed_denominator(self):
        fixture = fixtures.SignalsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        code = fixture.quarter_fixture('2026-09-05');source = fixture.source()
        self.assertEqual(source.quarter('2026-09-05', code)['业绩_可评估家数'], 0)
        self.assertEqual(source.quarter('2026-09-06', code)['业绩_可评估家数'], 1)
        self.assertEqual(source.quarter_details[('2026-09-06', code)]['已上市分母'], 2)

    def test_real_listing_boundary_and_counts_are_semantic_notes(self):
        self.assertEqual(necessary_time('v;L=2026-09-01;N=10;U=5;生成时间=今天'), ('L=2026-09-01', 'N=10', 'U=5'))


class ListingLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root = Path(self.tmp.name)
        self.path = self.root / 'data/财报/上市日期.jsonl';self.path.parent.mkdir(parents=True)

    def save(self, rows):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_network_failure_does_not_erase_known_listing_date(self):
        first = listing('000001', '1991-04-03')
        unknown = listing('000001', '', at='2026-10-31T10:00:00+08:00', status='未知，不进入分母')
        self.save([first, unknown])
        self.assertEqual(cov.read_listings(self.root)['000001']['上市日期'], '1991-04-03')

    def test_late_arriving_old_fact_does_not_override_newer_correction(self):
        first = listing('000001', '1991-04-03')
        corrected = listing('000001', '1991-04-04', at='2026-10-31T10:00:00+08:00')
        self.save([corrected, first])
        self.assertEqual(cov.read_listings(self.root)['000001']['上市日期'], '1991-04-04')

    def test_same_observation_time_conflict_future_or_fake_source_is_rejected(self):
        first = listing('000001', '1991-04-03')
        for value in (dict(first, 上市日期='1991-04-04'), dict(first, 上市日期='2026-11-01'),
                      dict(first, 来源='https://example.com/000001'), dict(first, 采集时间='2026-10-30T10:00:00')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.save([first, value]);cov.read_listings(self.root)


class FormalCollectorBoundaryTests(unittest.TestCase):
    def test_financial_http_is_actions_only(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(RuntimeError, '只允许 GitHub Actions'):
            batch.HTTP(10**9)

    def test_probe_universe_rejects_cross_industry_duplicate(self):
        sectors={str(i):{'来源':batch.SW_COMPONENT,'完整总数':1,'rows':[{'证券代码':str(i).zfill(6)}]} for i in range(27)}
        report={'状态':'探测通过','正式数据未写入':True,'完整候选数':27,'错误':[],
                '跨行业重复':{},'独立股票数':27,'成分':sectors}
        self.assertEqual(len(batch.probe_universe(report)[0]),27)
        sectors['1']['rows'][0]['证券代码']=sectors['0']['rows'][0]['证券代码']
        with self.assertRaisesRegex(ValueError,'重复'):
            batch.probe_universe(report)

    def test_legacy_non_candidate_does_not_survive_new_27_scope(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cov, 'candidate_industries', return_value={'801010':'农林牧渔'}):
            legacy={'801010':{'date':'2026-01-01','rows':[{'证券代码':'000001'}]},
                    '801960':{'date':'2026-01-01','rows':[{'证券代码':'600028'}]}}
            self.assertEqual(set(cov.read_members(Path(directory), legacy)), {'801010'})


class CoverageSideTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root = Path(self.tmp.name)
        self.row = {'date': '2026-09-01', 'code': '801080', '行业': '合成行业', '季报期': '2026-06-30', '规则可见日': '2026-09-01',
                    '当前成分总量': 20, '已上市分母': 10, '未上市家数': 5, '上市日期未知家数': 5,
                    '可评估家数': 10, '加速家数': 4, '季报覆盖率': 1, '行业利润加速占比': .4, '说明': '合成输入'}

    def test_only_real_changes_append_and_count_decomposition_is_enforced(self):
        changes, report = cov.plan_coverage_table(self.root, [self.row], '2026-10-31T10:00:00+08:00')
        cov_path, content = next(iter(changes.items()));cov_path.parent.mkdir(parents=True);cov_path.write_bytes(content)
        changes, report = cov.plan_coverage_table(self.root, [self.row], '2026-11-01T10:00:00+08:00')
        self.assertEqual(changes, {});self.assertEqual(report['追加行'], 0)
        with self.assertRaisesRegex(ValueError, '恒等式'):
            cov.plan_coverage_table(self.root, [dict(self.row, 上市日期未知家数=6)], 'later')

    def test_zero_denominator_is_missing_and_invalid_counts_rejected(self):
        row = dict(self.row, 已上市分母=0, 未上市家数=15, 可评估家数=0, 加速家数=None, 季报覆盖率=None, 行业利润加速占比=None)
        self.assertEqual(cov.plan_coverage_table(self.root, [row], 'stamp')[1]['追加行'], 1)
        with self.assertRaisesRegex(ValueError, '比例'):
            cov.plan_coverage_table(self.root, [dict(row, 季报覆盖率=0)], 'stamp')
        with self.assertRaisesRegex(ValueError, '越界'):
            cov.plan_coverage_table(self.root, [dict(row, 可评估家数=1)], 'stamp')

    def test_fifty_mb_rule_only_compacts_derived_side_table(self):
        changes, _ = cov.plan_coverage_table(self.root, [self.row], 'first')
        path, content = next(iter(changes.items()));path.parent.mkdir(parents=True);path.write_bytes(content)
        with patch.object(cov, 'SIDE_LIMIT', 1):
            changes, report = cov.plan_coverage_table(self.root, [dict(self.row, 加速家数=5, 行业利润加速占比=.5)], 'next')
        self.assertTrue(report['整表重算覆盖'])
        self.assertEqual(len(list(csv.DictReader(io.StringIO(changes[path].decode())))), 1)

    def test_signal_budget_failure_does_not_promote_side_table(self):
        path = self.root / 'data/财报/覆盖率明细.csv'
        with self.assertRaisesRegex(ValueError, '100MiB'):
            write_revisions(self.root, {}, {'28': {'允许追加': False}}, extra_changes={path: b'new'})
        self.assertFalse(path.exists())

    def real_fixture(self):
        fixture = fixtures.SignalsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.quarter_fixture();fixture.flow()
        now = dt.datetime(2026, 10, 30, 17, tzinfo=cov.BJ)
        fixtures.sig.run(fixture.root, now=now)
        return fixture, now

    def protected(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for folder in ('data/主线', 'data/财报')
                for p in (root / folder).rglob('*') if p.is_file()}

    def test_primary_and_side_repeat_zero_and_failure_rolls_both_back(self):
        fixture, now = self.real_fixture()
        old = self.protected(fixture.root)
        result = fixtures.sig.run(fixture.root, now=now + dt.timedelta(hours=1))
        self.assertEqual(result['覆盖率侧表']['追加行'], 0)
        self.assertTrue(all(n == 0 for n in result['新增行'].values()))
        self.assertTrue(all((fixture.root / p).read_bytes() == b for p, b in old.items() if p.endswith('.csv')))
        ledger = fixture.root / 'data/财报/上市日期.jsonl'
        ledger.write_text(ledger.read_text() + json.dumps(listing('000001', '2026-09-02', at='2026-10-31T10:00:00+08:00')) + '\n')
        before = self.protected(fixture.root)
        import earnings_history_transaction as transaction
        replace = transaction.os.replace;calls = [0]
        def fail_second(source, target):
            if str(source).endswith('.new'):
                calls[0] += 1
                if calls[0] == 2:
                    raise OSError('模拟主线/侧表晋升中途失败')
            return replace(source, target)
        with patch.object(transaction.os, 'replace', side_effect=fail_second), self.assertRaises(OSError):
            fixtures.sig.run(fixture.root, now=now)
        self.assertEqual(before, self.protected(fixture.root))

    def test_budget_or_invalid_funding_leaves_primary_and_side_as_previous_success(self):
        fixture, now = self.real_fixture()
        before = self.protected(fixture.root)
        import pandas as pd
        flow = fixture.root / 'data/资金轮动/日表.csv';frame = pd.read_csv(flow, dtype={'code': str})
        frame.loc[frame.date == '2026-10-29', '占比_31'] = float('nan');frame.to_csv(flow, index=False)
        result = fixtures.sig.run(fixture.root, now=now)
        self.assertEqual(result['status'], '失败')
        self.assertFalse(result['覆盖率侧表']['已提交'])
        self.assertEqual(before, self.protected(fixture.root))
        frame.loc[frame.date == '2026-10-29', '占比_31'] = .25;frame.to_csv(flow, index=False)
        with patch('mainline_signal_store.LIMIT', 1), self.assertRaisesRegex(ValueError, '100MiB'):
            fixtures.sig.run(fixture.root, now=now)
        self.assertEqual(before, self.protected(fixture.root))


if __name__ == '__main__':
    unittest.main()
