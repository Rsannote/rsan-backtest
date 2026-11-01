"""T1 离线边界测试：只用模拟响应，不访问行情接口。"""
import importlib.util
import json
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

SPEC = importlib.util.spec_from_file_location("hangqing", Path(__file__).resolve().parents[1] / "scripts/update_hangqing.py")
hq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hq)


def response(day="2024-01-02"):
    return json.dumps({"code": 0, "data": {"sz159611": {"day": [[day, "1", "1.1", "1.2", ".9", "100"]]}}})


def error(code):
    return urllib.error.HTTPError("https://example.test/", code, "模拟错误", {}, None)


class HistoryTests(unittest.TestCase):
    def test_unknown_year_skips_501_and_empty_prefix(self):
        notes = []
        with patch.object(hq, "get", side_effect=[error(501), '{"code":0,"data":{}}', response("2024-02-01")]) as get:
            df = hq.fetch_raw("sz159611", date(2018, 1, 1), empty_prefix=True, notes=notes)
        self.assertEqual(list(df.index.strftime("%Y-%m-%d")), ["2024-02-01"])
        self.assertEqual(len(notes), 2)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(list(df.iloc[0]), [1, 1.2, .9, 1.1, 100])

    def test_incremental_501_is_failure(self):
        with patch.object(hq, "get", side_effect=error(501)) as get:
            with self.assertRaises(urllib.error.HTTPError):
                hq.fetch_raw("sz159611", date(2024, 1, 1))
        self.assertNotIn("empty_501", get.call_args.kwargs)

    def test_501_after_valid_history_is_failure(self):
        with patch.object(hq, "get", side_effect=[response("2021-01-04"), error(501)]):
            with self.assertRaises(urllib.error.HTTPError):
                hq.fetch_raw("sz159611", date(2021, 1, 1), empty_prefix=True)

    def test_non_501_errors_and_bad_json_are_not_swallowed(self):
        for result in (error(429), error(403), error(503), TimeoutError("模拟超时"), "不是 JSON", '{"code":-1,"msg":"接口错误"}'):
            with self.subTest(result=result), patch.object(hq, "get", side_effect=[result]):
                with self.assertRaises(Exception):
                    hq.fetch_raw("sz159611", date(2024, 1, 1), empty_prefix=True)

    def test_full_starts_at_stored_tencent_first_year(self):
        old = pd.DataFrame(index=pd.to_datetime(["2022-01-07", "2024-01-02"]))
        notes = []
        with patch.object(hq, "read_store", return_value=old), patch.object(hq, "fetch_raw") as fetch:
            hq.fetch_full("sz159611", "任意.csv", notes)
        fetch.assert_called_once_with("sz159611", date(2022, 1, 1))
        self.assertIn("腾讯", notes[0])

    def test_unknown_year_uses_prefix_fallback(self):
        with patch.object(hq, "read_store", return_value=None), patch.object(hq, "fetch_raw") as fetch:
            hq.fetch_full("sz159611", "任意.csv", [])
        self.assertTrue(fetch.call_args.kwargs["empty_prefix"])

    def test_all_empty_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(hq, "LIB", temp), patch.object(hq, "EVD", temp), patch.object(hq, "get", return_value='{"code":0,"data":{}}'):
                rep = hq.update_one({"code": "159611", "type": "ETF", "name": "测试"}, False)
            self.assertEqual(rep["status"], "失败")
            self.assertFalse(list(Path(temp).iterdir()))

    def test_existing_file_survives_network_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ETF/159611.csv"
            path.parent.mkdir()
            data = b"date,open,high,low,close,volume,factor\n2024-01-02,1,1,1,1,100,1\n"
            path.write_bytes(data)
            with patch.object(hq, "LIB", temp), patch.object(hq, "get", side_effect=error(501)):
                rep = hq.update_one({"code": "159611", "type": "ETF", "name": "测试"}, True)
            self.assertEqual(rep["status"], "失败")
            self.assertEqual(path.read_bytes(), data)


class RetryTests(unittest.TestCase):
    """模拟时钟和 HTTP 层，真正执行 get() 的节流与重试，不联网也不等待。"""

    def setUp(self):
        self.now = hq.time.time()
        self.waits = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(hq, "_next", [0.0]))
        stack.enter_context(patch.object(hq.time, "time", side_effect=lambda: self.now))
        stack.enter_context(patch.object(hq.time, "sleep", side_effect=self.advance))

    def advance(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def test_transient_prefix_501_retries_same_segment_and_keeps_history(self):
        reply = MagicMock()
        reply.__enter__.return_value.read.return_value = response().encode()
        with patch.object(hq.urllib.request, "urlopen", side_effect=[error(501), reply]) as request:
            df = hq.fetch_raw("sz159611", date(2024, 1, 1), empty_prefix=True)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[0].full_url, request.call_args_list[1].args[0].full_url)
        self.assertEqual(self.waits, [30])
        self.assertEqual(list(df.index.strftime("%Y-%m-%d")), ["2024-01-02"])

    def test_prefix_501_only_escapes_after_three_backoff_attempts(self):
        with patch.object(hq.urllib.request, "urlopen", side_effect=error(501)) as request:
            with self.assertRaises(urllib.error.HTTPError):
                hq.get("https://example.test/", {}, tries=hq.PREFIX_TRIES, empty_501=True)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.waits, [30, 60])

    def test_mixed_network_errors_never_become_empty_prefix(self):
        with patch.object(hq.urllib.request, "urlopen", side_effect=[error(501), TimeoutError("模拟超时"), error(501)]):
            with self.assertRaises(RuntimeError):
                hq.get("https://example.test/", {}, tries=3, empty_501=True)

    def test_normal_501_still_retries_five_times_then_fails(self):
        with patch.object(hq.urllib.request, "urlopen", side_effect=error(501)) as request:
            with self.assertRaises(RuntimeError):
                hq.get("https://example.test/", {})
        self.assertEqual(request.call_count, 5)


class PrefixGuardTests(unittest.TestCase):
    def test_reprobe_recovers_previous_segment(self):
        notes = []
        with patch.object(hq, "get", side_effect=['{"code":0,"data":{}}', response(), response("2021-06-15")]) as get:
            df = hq.fetch_raw("sz159611", date(2021, 1, 1), empty_prefix=True, notes=notes)
        self.assertEqual(list(df.index.strftime("%Y-%m-%d")), ["2021-06-15", "2024-01-02"])
        self.assertEqual(get.call_args_list[0].args[0], get.call_args_list[2].args[0])
        self.assertIn("重新核验", notes[-1])

    def test_reprobe_keeps_going_back_if_recovered_data_also_touches_boundary(self):
        with patch.object(hq, "get", side_effect=['{"code":0,"data":{}}', '{"code":0,"data":{}}', response(), response("2021-01-04"), response("2018-06-15")]):
            df = hq.fetch_raw("sz159611", date(2018, 1, 1), empty_prefix=True)
        self.assertEqual(list(df.index.strftime("%Y-%m-%d")), ["2018-06-15", "2021-01-04", "2024-01-02"])

    def test_reprobe_empty_or_exhausted_501_is_uncertain_and_fails(self):
        for uncertain in ('{"code":0,"data":{}}', error(501)):
            with self.subTest(uncertain=uncertain), patch.object(hq, "get", side_effect=['{"code":0,"data":{}}', response(), uncertain]):
                with self.assertRaisesRegex(RuntimeError, "前缀重探仍无法确认"):
                    hq.fetch_raw("sz159611", date(2021, 1, 1), empty_prefix=True)

    def test_ten_day_boundary_is_inclusive(self):
        for day, expected_calls in (("2024-01-11", 3), ("2024-01-12", 2)):
            with self.subTest(day=day), patch.object(hq, "get", side_effect=['{"code":0,"data":{}}', response(day), response("2021-06-15")]) as get:
                hq.fetch_raw("sz159611", date(2021, 1, 1), empty_prefix=True)
            self.assertEqual(get.call_count, expected_calls)

    def test_uncertain_prefix_does_not_write_price_or_events(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "分红表/159611.json"
            event.parent.mkdir()
            event.write_text("[]", encoding="utf-8")
            with patch.object(hq, "LIB", temp), patch.object(hq, "EVD", str(event.parent)), patch.object(hq, "get", side_effect=['{"code":0,"data":{}}', response("2008-01-04"), '{"code":0,"data":{}}']), patch.object(hq, "write_store") as write:
                rep = hq.update_one({"code": "159611", "type": "ETF", "name": "测试"}, False)
            self.assertEqual(rep["status"], "失败")
            self.assertIn("前缀重探仍无法确认", rep["notes"][-1])
            write.assert_not_called()
            self.assertFalse((Path(temp) / "ETF/159611.csv").exists())
            self.assertEqual(event.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()


class OverseasTests(unittest.TestCase):
    """T3：雅虎海外指数解析与路由（离线模拟）。"""

    def chart(self, stamps, closes, end=None, off=-14400):
        return {"meta": {"gmtoffset": off, "currentTradingPeriod": {"regular": {"end": end}} if end else {}},
                "timestamp": stamps,
                "indicators": {"quote": [{"open": closes, "high": closes, "low": closes, "close": closes,
                                          "volume": [None] * len(closes)}]}}

    def test_local_date_none_rows_and_volume(self):
        # 2026-10-30 09:30 美东 = 13:30 UTC；gmtoffset -4h → 当地 10-30
        df = hq.parse_yahoo(self.chart([1793367000, 1793453400], [100.0, None]), 1793800000)
        self.assertEqual(list(df.index.strftime("%Y-%m-%d")), ["2026-10-30"])
        self.assertEqual(df.iloc[0]["volume"], 0)

    def test_unfinished_bar_dropped_until_grace_passes(self):
        open_ts, end = 1793367000, 1793390400            # 10-30 开盘与 16:00 收盘（美东）
        chart = self.chart([open_ts], [100.0], end=end)
        self.assertTrue(hq.parse_yahoo(chart, end - 60).empty)                       # 盘中
        self.assertTrue(hq.parse_yahoo(chart, end + hq.CLOSE_GRACE - 1).empty)       # 刚收盘
        self.assertEqual(len(hq.parse_yahoo(chart, end + hq.CLOSE_GRACE + 1)), 1)    # 收盘 30 分钟后

    def test_routing_yahoo_vs_tencent(self):
        self.assertTrue(hq.is_yahoo({"code": "^IXIC", "group": "海外指数"}))
        self.assertFalse(hq.is_yahoo({"code": "hkHSTECH", "group": "海外指数"}))
        self.assertFalse(hq.is_yahoo({"code": "sh000300", "group": "指数"}))
        self.assertEqual(hq.symbol("hkHSTECH", "指数"), "hkHSTECH")

    def test_yahoo_error_raises_and_full_update_writes_from_2010(self):
        with patch.object(hq, "get", return_value=json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}})):
            with self.assertRaises(RuntimeError):
                hq.fetch_yahoo("^XXX", date(2010, 1, 1))
        stamps = [1262615400, 1262701800]                 # 2010-01-04、01-05 美东开盘
        body = json.dumps({"chart": {"result": [self.chart(stamps, [2300.0, 2310.0], off=-18000)]}})
        with tempfile.TemporaryDirectory() as tmp, patch.object(hq, "LIB", tmp), patch.object(hq, "get", return_value=body) as get:
            rep = hq.update_one({"code": "^IXIC", "name": "纳斯达克综合", "type": "指数", "group": "海外指数"}, False)
            self.assertEqual(rep["status"], "ok", rep)
            self.assertIn("period1=1262304000", get.call_args[0][0])
            saved = pd.read_csv(Path(tmp) / "指数" / "^IXIC.csv")
            self.assertEqual(list(saved["date"]), ["2010-01-04", "2010-01-05"])
            self.assertEqual(set(saved["factor"]), {1})


class OverseasListTests(unittest.TestCase):
    def test_six_overseas_indices_listed_with_source_note(self):
        import csv as _csv
        text = (Path(__file__).resolve().parents[1] / "data/标的清单.csv").read_text(encoding="utf-8")
        rows = [r for r in _csv.DictReader(text.splitlines()) if r["group"] == "海外指数"]
        self.assertEqual([r["code"] for r in rows], ["^IXIC", "^GSPC", "^N225", "^KS11", "^HSI", "hkHSTECH"])
        self.assertTrue(all(r["type"] == "指数" and r["note"].startswith("来源") and "日期为" in r["note"] for r in rows))
