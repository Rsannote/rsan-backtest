"""已验证的同花顺HTTPS路径迁移：不会请求HTTP或其他代码/域名。"""
import sys
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from earnings_coverage_batch import HTTP


class RedirectTests(TestCase):
    def client(self, location, second_status=200):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}):
            client = HTTP(time.monotonic() + 300)
        first = Mock(status_code=301, headers={'Location': location})
        second = Mock(status_code=second_status, url='https://basic.10jqka.com.cn/000002/finance.html',
                      content=b'raw', text='raw', apparent_encoding='utf-8')
        client.session = Mock()
        client.session.get.side_effect = [first, second]
        return client

    def test_exact_same_stock_path_upgrade_keeps_tls_and_raw_bytes(self):
        client = self.client('http://basic.10jqka.com.cn/000002/finance.html')
        self.assertEqual(client.get('https://basic.10jqka.com.cn/new/000002/finance.html'), ('raw', b'raw'))
        for call in client.session.get.call_args_list:
            self.assertTrue(call.args[0].startswith('https://'))
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertNotEqual(call.kwargs.get('verify'), False)

    def test_other_stock_host_port_credentials_or_query_rejected_before_request(self):
        for url in ('http://basic.10jqka.com.cn/600519/finance.html',
                    'https://evil.example/000002/finance.html',
                    'https://basic.10jqka.com.cn.evil.example/000002/finance.html',
                    'https://basic.10jqka.com.cn:123/000002/finance.html',
                    'https://user@basic.10jqka.com.cn/000002/finance.html',
                    'https://basic.10jqka.com.cn/000002/finance.html?x=1'):
            with self.subTest(url=url):
                client = self.client(url)
                with self.assertRaises(ValueError):
                    client.get('https://basic.10jqka.com.cn/new/000002/finance.html')
                self.assertEqual(client.session.get.call_count, 1)

    def test_second_redirect_rejected_and_sina_does_not_inherit_ths_exception(self):
        client = self.client('http://basic.10jqka.com.cn/000002/finance.html', 302)
        with self.assertRaises(ValueError):
            client.get('https://basic.10jqka.com.cn/new/000002/finance.html')
        client = self.client('http://basic.10jqka.com.cn/000002/finance.html')
        with self.assertRaises(ValueError):
            client.get('https://vip.stock.finance.sina.com.cn/corp/000002')
