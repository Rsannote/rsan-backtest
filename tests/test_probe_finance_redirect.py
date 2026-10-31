"""T13c 财报重定向诊断边界；所有响应为合成数据。"""
from pathlib import Path
import sys
import unittest

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import probe_finance_redirect as probe


class Response:
    def __init__(self, status, *, location='', body=b''):
        self.status_code = status
        self.headers = {'Location': location} if location else {}
        self.content = body
        self.text = body.decode()
        self.apparent_encoding = 'utf-8'

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('HTTP ' + str(self.status_code))


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append((url, kwargs))
        return self.responses.pop(0)


class RedirectProbeTests(unittest.TestCase):
    def test_http_location_is_never_requested_and_https_equivalent_is_tried(self):
        session = Session([Response(302, location='http://basic.10jqka.com.cn/000002/finance.html'),
                           Response(500)])
        result = probe.trace(session, '000002')
        self.assertTrue(result['曾HTTPS升级'])
        self.assertEqual(session.urls[1][0], 'https://basic.10jqka.com.cn/000002/finance.html')
        self.assertTrue(all(url.startswith('https://') for url, _ in session.urls))
        self.assertFalse(session.urls[0][1]['allow_redirects'])

    def test_cross_provider_redirect_is_recorded_but_not_followed(self):
        session = Session([Response(302, location='https://example.com/steal')])
        result = probe.trace(session, '000002')
        self.assertEqual(result['状态'], '拒绝跨提供方重定向')
        self.assertEqual(len(session.urls), 1)

    def test_non_http_scheme_and_excessive_hops_are_bounded(self):
        session = Session([Response(302, location='javascript:alert(1)')])
        self.assertEqual(probe.trace(session, '000002')['状态'], '拒绝跨提供方重定向')
        looping = Session([Response(302, location='/again') for _ in range(2)])
        self.assertEqual(probe.trace(looping, '000002', max_hops=1)['状态'], '重定向过多')

    def test_public_url_drops_query_values(self):
        self.assertEqual(probe.public_url('https://a.10jqka.com.cn/x?token=secret'),
                         {'scheme': 'https', 'host': 'a.10jqka.com.cn', 'path': '/x', 'has_query': True})


if __name__ == '__main__':
    unittest.main()
