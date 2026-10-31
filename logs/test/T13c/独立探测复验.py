"""T13c独立反例；仅合成响应与本地临时目录，不执行财经HTTP。"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import probe_earnings_coverage as p
from earnings_universe import THS_COMPANY, SW_COMPONENT, candidate_industries, component_page, listing_date


def obj(code):
    return {'stockcode': code, 'stockname': '合成测试公司', 'newweight': '1.0', 'beginningdate': '2021-12-13'}


def company(body, code='600001'):
    return '<title>合成测试公司(' + code + ')-公司资料</title><p>' + body + '</p>'


class IndependentProbeTests(unittest.TestCase):
    def parse(self, text):
        return listing_date(company(text), '600001', THS_COMPANY.format(code='600001'), today=dt.date(2026, 10, 31))

    def test_b_share_only_cannot_become_a_share_ipo(self):
        with self.assertRaises(ValueError):
            self.parse('B股上市日期：1993-01-01')

    def test_h_share_only_cannot_become_a_share_ipo(self):
        with self.assertRaises(ValueError):
            self.parse('H股上市日期：1993-01-01')

    def test_malformed_date_cannot_be_silently_truncated(self):
        for text in ('上市日期：2001-08-270', '上市日期：2001-08-27-1'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(text)

    def test_nonfinite_weight_does_not_become_valid_component_number(self):
        for weight in (float('nan'), float('inf'), 'NaN', 'Infinity'):
            row = obj('600001'); row['newweight'] = weight
            with self.subTest(weight=repr(weight)), self.assertRaises(ValueError):
                component_page({'data': {'count': 1, 'results': [row]}})

    def fake_probe(self, mode):
        names = candidate_industries(ROOT)
        first = next(iter(names))
        positions = {c: n for n, c in enumerate(names)}
        class Fake:
            def __init__(self):
                self.trace = []
            def get(self, url, key):
                if key.startswith('申万_'):
                    _, code, page = key.split('_')
                    stock = str(600000 + positions[code])
                    count, rows = 1, [obj(stock)]
                    if mode == 'cross-duplicate':
                        rows = [obj('600001')]
                    if code == first and mode in ('drift', 'empty-next'):
                        count = 2
                        if page == '2':
                            if mode == 'drift':
                                count, rows = 3, [obj('600999')]
                            else:
                                rows = []
                    return json.dumps({'data': {'count': count, 'results': rows}})
                code = key.rsplit('_', 1)[1]
                return company('上市日期：2001-08-27', code)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = root/'data/保护.bin'; protected.parent.mkdir(); protected.write_bytes('原字节\r\n'.encode())
            with patch.object(p, 'candidate_industries', return_value=names), patch.object(p, 'current_legacy_members', return_value={}):
                report = p.probe(root, client=Fake())
            self.assertEqual(protected.read_bytes(), '原字节\r\n'.encode())
            self.assertEqual(list((root/'data').iterdir()), [protected])
            return report

    def test_total_drift_rejects_just_that_sector_and_fails_whole_probe(self):
        r = self.fake_probe('drift')
        self.assertEqual(r['完整候选数'], 26)
        self.assertEqual(r['状态'], '探测失败')
        self.assertTrue(any('总数变化' in e['错误'] for e in r['错误']))

    def test_empty_following_page_is_not_a_complete_sector(self):
        r = self.fake_probe('empty-next')
        self.assertEqual(r['完整候选数'], 26)
        self.assertEqual(r['状态'], '探测失败')

    def test_cross_sector_duplicate_cannot_pass_even_with_27_complete_pages(self):
        r = self.fake_probe('cross-duplicate')
        self.assertEqual(r['完整候选数'], 27)
        self.assertEqual(r['状态'], '探测失败')
        self.assertEqual(len(r['跨行业重复']['600001']), 27)

    def test_local_guard_precedes_session_creation(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(p.requests, 'Session') as session:
            with self.assertRaises(RuntimeError):
                p.ProbeHTTP(time.monotonic() + 100, '/tmp/no-ca', '/tmp/no-output')
            session.assert_not_called()

    def test_https_ca_raw_bytes_and_deadline_are_enforced_without_network(self):
        raw = company('上市日期：2001-08-27').encode() + b'\r\n'
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); out = base/'raw'; ca = base/'ca.pem'
            session = Mock()
            response = Mock(content=raw, apparent_encoding='utf-8', text=raw.decode(), status_code=200)
            session.get.return_value = response
            with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), patch.object(p.requests, 'Session', return_value=session), patch.object(p.time, 'sleep'), patch.object(p.signal, 'signal', return_value=signal.SIG_DFL), patch.object(p.signal, 'alarm') as alarm:
                client = p.ProbeHTTP(time.monotonic()+500, ca, out, interval=0)
                sw = SW_COMPONENT+'?swindexcode=801010&page=1&page_size=1000'
                response.url = sw
                client.get(sw, 'sw')
                self.assertEqual(session.get.call_args.kwargs['verify'], str(ca))
                self.assertEqual(session.get.call_args.kwargs['timeout'], (10, 20))
                self.assertEqual((out/'sw.bin').read_bytes(), raw)
                self.assertEqual(client.trace[-1]['SHA256'], hashlib.sha256(raw).hexdigest())
                self.assertIn(unittest.mock.call(40), alarm.call_args_list)
                self.assertEqual(alarm.call_args, unittest.mock.call(0))
                ths = THS_COMPANY.format(code='600519'); response.url = ths
                client.get(ths, 'ths')
                self.assertIs(session.get.call_args.kwargs['verify'], True)
                response.url = 'http://example.invalid/redirect'
                with self.assertRaisesRegex(ValueError, '重定向'):
                    client.get(ths, 'redirect')
                self.assertTrue(client.trace[-1]['错误'])
                self.assertEqual((out/'redirect.bin').read_bytes(), raw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
