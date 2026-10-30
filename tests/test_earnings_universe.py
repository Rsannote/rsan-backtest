"""T13c来源边界：不猜总数、不混上市与计入、不把空页视为完整。"""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from earnings_universe import (SINA_COMPANY, THS_COMPANY, candidate_industries,
                               component_page, listing_date)
from probe_earnings_coverage import ProbeHTTP, SAMPLES, probe


def row(code='600001', name='合成公司'):
    return {'stockcode': code, 'stockname': name, 'newweight': '0.1', 'beginningdate': '2021-12-13'}


class UniverseSourceTests(unittest.TestCase):
    def test_component_exact_fields_keep_index_inclusion_separate(self):
        rows, count = component_page({'data': {'count': '1', 'results': [row()]}})
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]['计入日期'], '2021-12-13')
        self.assertNotIn('上市日期', rows[0])
        self.assertEqual(rows[0]['证券代码'], '600001')

    def test_zero_weight_is_not_unknown(self):
        value = row(); value['newweight'] = 0
        rows, _ = component_page({'data': {'count': 1, 'results': [value]}})
        self.assertEqual(rows[0]['最新权重'], '0')

    def test_nonfinite_or_wrongly_typed_weights_are_rejected(self):
        for weight in (float('nan'), float('inf'), '-inf', 'NaN', 'Infinity', True, {}):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                value = row(); value['newweight'] = weight
                component_page({'data': {'count': 1, 'results': [value]}})

    def test_missing_total_and_empty_or_wrong_shapes_fail(self):
        for data in ({'results': [row()]}, {'count': 0, 'results': []}, {'count': True, 'results': [row()]},
                     {'count': '1.5', 'results': [row()]}, {'count': 1, 'results': None}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                component_page({'data': data})

    def test_disagreeing_total_duplicate_invalid_codes_fail(self):
        for data in ({'count': 1, 'total': 2, 'results': [row()]},
                     {'count': 2, 'results': [row(), row()]},
                     {'count': 1, 'results': [row('600001.SH')]},
                     {'count': 1, 'results': [row(name='')]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                component_page({'data': data})

    def html(self, body, code='600001'):
        return '<html><title>合成公司(' + code + ')-公司资料</title>' + body + '</html>'

    def parse(self, body, *, code='600001', source=None):
        return listing_date(self.html(body, code=code), code, source or THS_COMPANY.format(code=code), today=dt.date(2026, 10, 31))

    def test_label_and_nested_markup_keep_raw_date(self):
        r = self.parse('<table><tr><th>上市日期：</th><td><b>2001-8-27</b></td></tr></table>')
        self.assertEqual(r['上市日期'], '2001-08-27')
        self.assertEqual(r['原始日期'], '2001-8-27')
        self.assertEqual(r['来源字段'], '上市日期')

    def test_chinese_date_and_sina_page(self):
        r = self.parse('<p>A股上市时间：1991年4月3日</p>', source=SINA_COMPANY.format(code='600001'))
        self.assertEqual(r['上市日期'], '1991-04-03')

    def test_established_or_index_inclusion_dates_are_not_ipo(self):
        for body in ('成立日期：1990-01-01', '计入日期：2021-12-13', '首次交易日不详', '<script>上市日期：2001-08-27</script>'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.parse(body)

    def test_page_identity_error_is_not_missing_ipo(self):
        with self.assertRaisesRegex(ValueError, '身份'):
            listing_date(self.html('上市日期：2001-08-27', code='600002'), '600001', THS_COMPANY.format(code='600001'))

    def test_conflicting_dates_future_and_impossible_dates_fail(self):
        for body in ('上市日期：2001-08-27 A股上市日期：1999-01-01', '上市日期：2027-01-01', '上市日期：2026-02-30'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.parse(body)

    def test_b_and_h_share_listing_dates_cannot_be_used_for_a_share(self):
        for body in ('B股上市日期：1993-01-01', 'H股上市日期：1993-01-01', 'b 股 上市日期：1993-01-01'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.parse(body)
        self.assertEqual(self.parse('B股上市日期：1993-01-01 A股上市日期：2001-08-27')['上市日期'], '2001-08-27')

    def test_listing_date_must_be_a_complete_token(self):
        for body in ('上市日期：2001-08-270', '上市日期：2001-08-27.0', '上市日期：2001-08-27-2002-01-01'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.parse(body)

    def test_b_share_target_only_accepts_own_market_label(self):
        r = self.parse('A股上市日期：2001-08-27 B股上市日期：1993-01-01', code='200001')
        self.assertEqual(r['上市日期'], '1993-01-01')

    def test_duplicate_identical_listing_labels_are_consistent(self):
        self.assertEqual(self.parse('上市日期：2001-08-27 上市时间：2001/08/27')['上市日期'], '2001-08-27')

    def test_wrong_source_cannot_supply_listing_field(self):
        with self.assertRaisesRegex(ValueError, '来源'):
            self.parse('上市日期：2001-08-27', source='https://example.com/600001')

    def test_financial_http_rejected_on_local_host(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(RuntimeError, 'Actions'):
            ProbeHTTP(999999999, '/tmp/not-used', '/tmp/not-used')

    def test_actual_27_candidates_and_missing_14_not_13(self):
        names = candidate_industries(ROOT)
        old = json.loads((ROOT / 'data/v3/申万成分.json').read_text())
        self.assertEqual(len(names), 27)
        self.assertNotIn('801960', names)
        self.assertEqual(len(set(names) - set(old)), 14)

    def test_whole_probe_rejects_partial_pages_and_preserves_formal_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in ('system/行业清单.csv', 'data/v3/申万成分.json'):
                target = root / path; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / path).read_bytes())
            protected = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            class Fake:
                trace = []
                def get(self, url, key):
                    if key.startswith('申万_'):
                        return json.dumps({'data': {'count': 2, 'results': [row()]}})
                    return '<title>合成公司(' + key.rsplit('_', 1)[1] + ')</title><p>上市日期：2001-08-27</p>'
            report = probe(root, client=Fake())
            self.assertEqual(report['状态'], '探测失败')
            self.assertEqual(report['完整候选数'], 0)
            self.assertTrue(all(p.read_bytes() == value for p, value in protected.items()))


if __name__ == '__main__':
    unittest.main()
