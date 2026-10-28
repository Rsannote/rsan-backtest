"""合成夹具验证运行时段、交易日边界和真实产出格式；不联网、不接触正式数据。"""
import copy
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("health_check", Path(__file__).resolve().parents[1] / "scripts/health_check.py")
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)
MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "system/运行清单.json").read_text(encoding="utf-8"))


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = copy.deepcopy(MANIFEST)
        # 旧模式回归使用固定夹具，不能随正式清单的统计阈值漂移。
        legacy_grace = {"hangqing": 90, "market_state": 110, "news": 30, "community": 20,
                        "sentiment_index": 125, "tophub": 30, "pool": 30, "v3": 180,
                        "calendar_refresh": 0, "health_check": 0}
        for item in self.manifest["items"]:
            for key in ("check_mode", "max_age_minutes", "freshness_success_paths"):
                item.pop(key, None)
            item["grace_minutes"] = legacy_grace[item["id"]]
            item["trading_days_only"] = item["id"] in ("hangqing", "market_state", "community", "sentiment_index")
            if item.get("extra_schedule"):
                item["extra_schedule"]["grace_minutes"] = 90
        self.json("system/运行清单.json", self.manifest)
        # 明确的合成覆盖，用来验证边界，不是手填正式交易日数据。
        self.csv("data/行情库/指数/sh000300.csv", ["date", "close"], [["2026-09-30", "1"], ["2026-10-08", "1"], ["2026-10-23", "1"], ["2026-10-26", "1"], ["2026-10-27", "1"]])
        self.json("system/交易所休市.json", {"coverage_start": "2026-10-01", "coverage_end": "2026-11-30", "closed_dates": ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"], "sources": ["fixture"]})

    def put(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def json(self, path, obj):
        self.put(path, json.dumps(obj, ensure_ascii=False))

    def csv(self, path, columns, rows):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            writer.writerows(rows)

    def item(self, ident):
        return next(i for i in self.manifest["items"] if i["id"] == ident)

    def run_item(self, ident, now="2026-10-28T22:30:00+08:00"):
        return health.check_item(self.root, self.item(ident), health.timestamp(now), health.Calendar(self.root, self.manifest["calendar"]))

    def outputs(self, ident, date="2026-10-28"):
        for name in self.item(ident)["outputs"]:
            self.put(name.format(date=date), "夹具\n")

    def hangqing(self, date="2026-10-28", failed=()):
        rows = [["sh000300", "沪深300", date, date + " 18:00"]]
        rows += [[code, name, "2026-10-27", date + " 18:00"] for code, name in failed]
        self.csv("data/行情库/状态.csv", ["code", "name", "last", "updated"], rows)
        self.put("logs/行情库_最新.md", f"# 行情库更新 {date} 18:00（北京时间）\n\n- 标的 174 个，成功 {174-len(failed)}，失败 {len(failed)}，超时未轮到 0\n- 最新交易日：{date}\n\n## 失败\n\n" + "\n".join(f"- {code} {name}：RuntimeError: HTTPError 501: Not Implemented" for code, name in failed) + "\n\n## 后复权跳空超过21%\n- 000063 中兴通讯：1天\n")

    def news(self, run="2026-10-28 21:20", sources=None):
        self.outputs("news")
        self.json("data/新闻/状态.json", {"最近运行": run, "本次新增": 0, "来源": sources or {"新浪7x24": {"ok": True, "n": 0}, "华尔街见闻A股": {"ok": True, "n": 0}}})

    def mode(self, ident, mode, **kwargs):
        self.item(ident).update(check_mode=mode, **kwargs)

    def expected(self, ident, now):
        return health.expectation(self.item(ident), health.timestamp(now), health.Calendar(self.root, self.manifest["calendar"]))

    def tophub(self, runs):
        self.outputs("tophub")
        self.json("data/信息源/tophub/状态.json", {})
        self.put("data/信息源/tophub/运行记录.txt", "\n\n".join(f"## {stamp}\n{body}" for stamp, body in runs))

    def test_previous_mode_morning_is_strictly_previous_even_after_early_schedule(self):
        for ident in ("hangqing", "market_state", "sentiment_index", "community", "pool"):
            with self.subTest(item=ident):
                self.mode(ident, "previous_trading_day", times=["00:00"], grace_minutes=0)
                for now in ("2026-10-28T08:20:00+08:00", "2026-10-28T11:59:00+08:00"):
                    self.assertEqual(self.expected(ident, now)["data_date"], "2026-10-27")
                self.assertEqual(self.expected(ident, "2026-10-28T12:00:00+08:00")["data_date"], "2026-10-28")

    def test_previous_mode_evening_uses_calibrated_grace_before_today_is_due(self):
        self.mode("hangqing", "previous_trading_day", grace_minutes=420)
        self.hangqing("2026-10-27")
        row = self.run_item("hangqing", "2026-10-28T22:30:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        self.assertEqual(row["expected"]["data_date"], "2026-10-27")
        row = self.run_item("hangqing", "2026-10-28T23:40:00+08:00")
        self.assertEqual(row["status"], "延迟", row)
        self.assertEqual(row["expected"]["data_date"], "2026-10-28")

    def test_previous_mode_seven_hour_delay_crossing_midnight_covers_source_day(self):
        self.mode("market_state", "previous_trading_day", times=["18:30"], grace_minutes=420)
        self.outputs("market_state")
        self.json("data/市场状态/最新.json", {"数据日期": "2026-10-28", "生成时间": "2026-10-29 01:30", "数据告警": []})
        row = self.run_item("market_state", "2026-10-29T08:20:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        self.assertEqual(row["actual"]["data_date"], "2026-10-28")
        self.assertEqual(row["expected"]["data_date"], "2026-10-28")

    def test_previous_mode_weekend_holiday_and_pool_use_trading_calendar(self):
        self.mode("pool", "previous_trading_day")
        for now, expected in (("2026-10-25T08:20:00+08:00", "2026-10-23"),
                              ("2026-10-07T22:30:00+08:00", "2026-09-30"),
                              ("2026-10-28T08:20:00+08:00", "2026-10-27")):
            with self.subTest(now=now):
                self.assertEqual(self.expected("pool", now)["data_date"], expected)

    def test_previous_mode_pool_checks_source_date_without_previous_slot_timestamp(self):
        self.mode("pool", "previous_trading_day")
        self.outputs("pool")
        self.json("data/股票池/状态.json", {"数据日期": "2026-10-27", "最近检查": "2026-10-27 08:00", "文档数": {"stocks": 123}})
        self.assertEqual(self.run_item("pool", "2026-10-28T08:20:00+08:00")["status"], "正常")

    def test_previous_mode_saturday_rotation_waits_for_calibrated_deadline(self):
        self.mode("hangqing", "previous_trading_day")
        self.item("hangqing")["extra_schedule"]["grace_minutes"] = 420
        self.hangqing("2026-10-23")
        for now in ("2026-10-24T08:20:00+08:00", "2026-10-24T17:16:00+08:00"):
            row = self.run_item("hangqing", now)
            self.assertEqual(row["status"], "正常", row)
            self.assertNotIn("run_after", row["expected"])
        self.assertEqual(self.run_item("hangqing", "2026-10-24T17:17:00+08:00")["status"], "延迟")

    def test_previous_mode_failure_is_not_hidden_by_evening_grace(self):
        self.mode("hangqing", "previous_trading_day", grace_minutes=420)
        self.hangqing("2026-10-28", [("600872", "中炬高新")])
        row = self.run_item("hangqing")
        self.assertEqual(row["expected"]["data_date"], "2026-10-27")
        self.assertEqual(row["status"], "部分失败", row)

    def test_previous_mode_community_latest_failure_not_hidden_in_prior_folder(self):
        self.mode("community", "previous_trading_day", grace_minutes=420)
        for date, failure in (("2026-10-27", "无"), ("2026-10-28", "微博：HTTP500")):
            self.outputs("community", date)
            self.put(f"data/社区情绪/{date}/运行记录.txt", f"抓取时间：{date} 15:40（北京时间）\n成功：股吧（80行）\n失败：\n{failure}\n")
        row = self.run_item("community", "2026-10-28T22:30:00+08:00")
        self.assertEqual(row["expected"]["data_date"], "2026-10-27")
        self.assertEqual(row["actual"]["data_date"], "2026-10-28")
        self.assertEqual(row["status"], "部分失败", row)

    def test_freshness_morning_and_evening_ignore_slots_and_same_day(self):
        self.mode("news", "freshness", max_age_minutes=720)
        for now, last in (("2026-10-28T08:20:00+08:00", "2026-10-27 21:20"),
                           ("2026-10-28T22:30:00+08:00", "2026-10-28 16:00")):
            self.news(last)
            row = self.run_item("news", now)
            self.assertEqual(row["status"], "正常", row)
            self.assertEqual(row["expected"]["max_age_minutes"], 720)
            self.assertNotIn("data_date", row["expected"])
            self.assertNotIn("scheduled_at", row["expected"])

    def test_freshness_threshold_equal_is_healthy_and_one_second_over_is_delay(self):
        self.mode("news", "freshness", max_age_minutes=420)
        self.news("2026-10-28 01:20:00")
        self.assertEqual(self.run_item("news", "2026-10-28T08:20:00+08:00")["status"], "正常")
        row = self.run_item("news", "2026-10-28T08:20:01+08:00")
        self.assertEqual(row["status"], "延迟", row)
        self.assertGreater(row["actual"]["age_minutes"], 420)

    def test_freshness_accepts_seven_hours_across_midnight(self):
        self.mode("news", "freshness", max_age_minutes=420)
        self.news("2026-10-28 23:30")
        row = self.run_item("news", "2026-10-29T06:30:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        self.assertEqual(row["actual"]["age_minutes"], 420)

    def test_freshness_all_failed_never_counts_latest_attempt_as_success(self):
        self.mode("news", "freshness", max_age_minutes=720)
        self.news("2026-10-28 22:20", {"新浪": {"ok": False, "err": "timeout"}})
        row = self.run_item("news")
        self.assertEqual(row["status"], "失败", row)
        self.assertIsNone(row["actual"]["last_success_at"])
        self.assertIsNone(row["actual"]["age_minutes"])

    def test_freshness_all_failed_can_report_explicit_older_success(self):
        self.mode("news", "freshness", max_age_minutes=420)
        self.news("2026-10-28 22:20", {"新浪": {"ok": False, "err": "timeout"}})
        path = self.root / "data/新闻/状态.json"
        value = json.loads(path.read_text())
        value["last_success_at"] = "2026-10-28 12:00"
        self.json("data/新闻/状态.json", value)
        row = self.run_item("news")
        self.assertEqual(row["status"], "失败", row)
        self.assertEqual(row["actual"]["last_success_at"], "2026-10-28T12:00:00+08:00")
        self.assertEqual(row["actual"]["age_minutes"], 630)

    def test_freshness_partial_failures_win_even_with_recent_success(self):
        self.mode("news", "freshness", max_age_minutes=720)
        self.news(sources={"新浪": {"ok": True, "n": 10}, "巨潮": {"ok": False, "err": "HTTP500"}})
        row = self.run_item("news")
        self.assertEqual(row["status"], "部分失败", row)
        self.assertEqual(row["actual"]["last_success_at"], "2026-10-28T21:20:00+08:00")

    def test_freshness_tophub_uses_last_successful_log_block_after_total_failure(self):
        self.mode("tophub", "freshness", max_age_minutes=720)
        self.tophub([("2026-10-28 16:10", "旧成功\t20条\t新2"),
                     ("2026-10-28 22:10", "新失败\t失败\ttimeout")])
        row = self.run_item("tophub")
        self.assertEqual(row["status"], "失败", row)
        self.assertEqual(row["actual"]["last_success_at"], "2026-10-28T16:10:00+08:00")

    def test_freshness_tophub_cross_midnight_and_configured_state_path(self):
        self.mode("tophub", "freshness", max_age_minutes=720,
                  freshness_success_paths=[{"path": "data/信息源/tophub/状态.json", "field": "*.最近抓取"}])
        self.tophub([("2026-10-27 23:30", "成功\t20条\t新2")])
        self.assertEqual(self.run_item("tophub", "2026-10-28T08:20:00+08:00")["status"], "正常")
        self.tophub([("2026-10-28 08:10", "全挂\t失败\ttimeout")])
        self.json("data/信息源/tophub/状态.json", {"board": {"最近抓取": "2026-10-27 23:30"}})
        row = self.run_item("tophub", "2026-10-28T08:20:00+08:00")
        self.assertEqual(row["status"], "失败", row)
        self.assertEqual(row["actual"]["last_success_at"], "2026-10-27T23:30:00+08:00")

    def test_invalid_freshness_threshold_is_failed_instead_of_silent_grace(self):
        self.mode("news", "freshness", max_age_minutes=0)
        self.news()
        self.assertEqual(self.run_item("news")["status"], "失败")

    def test_weekend_uses_friday_without_optional_closures(self):
        (self.root / "system/交易所休市.json").unlink()
        self.hangqing("2026-10-23")
        row = self.run_item("hangqing", "2026-10-25T22:30:00+08:00")
        # 周六轮换依然被登记；本例移除额外时段以独立验证交易日更新。
        self.item("hangqing").pop("extra_schedule")
        row = self.run_item("hangqing", "2026-10-25T22:30:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        self.assertEqual(row["expected"]["data_date"], "2026-10-23")

    def test_known_historical_holiday_is_not_weekday(self):
        (self.root / "system/交易所休市.json").unlink()
        self.hangqing("2026-09-30")
        self.item("hangqing").pop("extra_schedule")
        row = self.run_item("hangqing", "2026-10-07T22:30:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        self.assertEqual(row["expected"]["data_date"], "2026-09-30")

    def test_current_trading_day_from_official_coverage(self):
        self.hangqing()
        self.assertEqual(self.run_item("hangqing")["status"], "正常")

    def test_missing_today_from_csv_does_not_mean_holiday(self):
        self.hangqing("2026-10-27")
        row = self.run_item("hangqing")
        self.assertEqual(row["status"], "延迟", row)
        self.assertEqual(row["expected"]["data_date"], "2026-10-28")

    def test_uncovered_weekday_is_unknown_and_failed(self):
        (self.root / "system/交易所休市.json").unlink()
        self.hangqing("2026-10-27")
        row = self.run_item("hangqing")
        self.assertEqual(row["status"], "失败", row)
        self.assertIn("无法确认", "".join(row["reasons"]))
        self.assertNotEqual(row["status"], "延迟")

    def test_before_close_deadline_still_expects_previous_day(self):
        self.hangqing("2026-10-27")
        row = self.run_item("hangqing", "2026-10-28T08:20:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        self.assertEqual(row["expected"]["data_date"], "2026-10-27")

    def test_four_501_failures_keep_code_name_and_partial_state(self):
        failed = [("600872", "中炬高新"), ("600874", "创业环保"), ("600875", "东方电气"), ("600887", "伊利股份")]
        self.hangqing(failed=failed)
        row = self.run_item("hangqing")
        self.assertEqual(row["status"], "部分失败", row)
        self.assertEqual(len(row["actual"]["failed"]), 4)
        for code, name in failed:
            self.assertIn(code + " " + name, "".join(row["reasons"]))
        self.assertNotIn("中兴通讯", "".join(row["reasons"]))

    def test_failures_are_not_hidden_by_late_data(self):
        self.hangqing("2026-10-27", [("600872", "中炬高新")])
        row = self.run_item("hangqing")
        self.assertEqual(row["status"], "部分失败", row)
        self.assertTrue(any("已到截止" in r for r in row["reasons"]))

    def test_all_failed_is_failure(self):
        self.news(sources={"新浪7x24": {"ok": False, "err": "timeout"}})
        self.assertEqual(self.run_item("news")["status"], "失败")

    def test_news_partial_failure_with_successful_sources(self):
        self.news(sources={"新浪7x24": {"ok": True, "n": 12}, "巨潮": {"ok": False, "err": "HTTP 500"}})
        row = self.run_item("news")
        self.assertEqual(row["status"], "部分失败", row)
        self.assertIn("巨潮", "".join(row["reasons"]))

    def test_news_same_day_does_not_satisfy_evening_slot(self):
        self.news("2026-10-28 18:42")
        row = self.run_item("news")
        self.assertEqual(row["status"], "延迟", row)
        self.assertIn("21:11", row["expected"]["scheduled_at"])

    def test_news_morning_slot_deadline_is_observed_at_0820(self):
        self.news("2026-10-27 21:20")
        row = self.run_item("news", "2026-10-28T08:20:00+08:00")
        self.assertEqual(row["status"], "延迟", row)
        self.assertIn("07:47", row["expected"]["scheduled_at"])
        self.assertIn("08:17", row["expected"]["deadline"])

    def test_news_zero_new_is_not_failure(self):
        self.news()
        self.assertEqual(self.run_item("news")["status"], "正常")

    def test_news_slot_before_and_at_deadline(self):
        self.news("2026-10-28 08:00")
        row = self.run_item("news", "2026-10-28T12:42:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        row = self.run_item("news", "2026-10-28T12:43:00+08:00")
        self.assertEqual(row["status"], "延迟", row)

    def test_news_bad_json_and_missing_file_are_failure(self):
        self.news()
        self.put("data/新闻/状态.json", "{")
        self.assertEqual(self.run_item("news")["status"], "失败")
        (self.root / "data/新闻/状态.json").unlink()
        self.assertEqual(self.run_item("news")["status"], "失败")

    def test_market_state_data_warning_is_partial(self):
        self.outputs("market_state")
        self.json("data/市场状态/最新.json", {"数据日期": "2026-10-28", "生成时间": "2026-10-28 18:20", "数据告警": ["融资余额来源超时"]})
        row = self.run_item("market_state")
        self.assertEqual(row["status"], "部分失败", row)

    def test_community_reads_existing_source_and_discussion_failures(self):
        self.outputs("community")
        self.put("data/社区情绪/2026-10-28/运行记录.txt", "抓取时间：2026-10-28 15:40（北京时间）\n成功：股吧（80行）\n失败：\n微博热搜：HTTP500\n讨论摘录：股吧1帖\n讨论摘录失败：\n集思录 timeout\n")
        row = self.run_item("community")
        self.assertEqual(row["status"], "部分失败", row)
        self.assertIn("集思录", "".join(row["reasons"]))

    def test_sentiment_is_registered_without_cron_and_stale_temperature_detected(self):
        self.outputs("sentiment_index")
        self.json("data/社区情绪/触发.json", {"date": "2026-10-28", "温度日期": "2026-10-27"})
        for name in ("温度计", "文本指标"):
            self.csv(f"data/社区情绪/{name}.csv", ["date", "value"], [["2026-10-28", "1"]])
        row = self.run_item("sentiment_index")
        self.assertEqual(row["status"], "部分失败", row)
        self.assertIn("workflow_run", self.item("sentiment_index")["trigger"])

    def test_sentiment_all_components_one_day_old_is_delay(self):
        self.outputs("sentiment_index")
        self.json("data/社区情绪/触发.json", {"date": "2026-10-27", "温度日期": "2026-10-27"})
        for name in ("温度计", "文本指标"):
            self.csv(f"data/社区情绪/{name}.csv", ["date", "value"], [["2026-10-27", "1"]])
        row = self.run_item("sentiment_index")
        self.assertEqual(row["status"], "延迟", row)

    def test_tophub_only_latest_run_failures_count(self):
        self.outputs("tophub")
        self.json("data/信息源/tophub/状态.json", {"a": {"最近抓取": "2026-10-28 21:10"}})
        self.put("data/信息源/tophub/运行记录.txt", "## 2026-10-28 16:10\n旧失败\t失败\ttimeout\n\n## 2026-10-28 21:10\n好榜单\t20条\t新2\n空榜单\t0条\t页面没有解析出条目\n新条目合计2\n")
        row = self.run_item("tophub")
        self.assertEqual(row["status"], "部分失败", row)
        self.assertIn("空榜单", "".join(row["reasons"]))
        self.assertNotIn("旧失败", "".join(row["reasons"]))

    def test_tophub_0820_has_not_passed_0837_cutoff(self):
        cal = health.Calendar(self.root, self.manifest["calendar"])
        result = health.expectation(self.item("tophub"), health.timestamp("2026-10-28T08:20:00+08:00"), cal)
        self.assertEqual(result["data_date"], "2026-10-27")
        self.assertIn("21:07", result["scheduled_at"])

    def test_pool_requires_last_check_time_even_when_data_date_is_today(self):
        self.outputs("pool")
        self.json("data/股票池/状态.json", {"数据日期": "2026-10-28", "最近检查": "2026-10-28 08:00", "文档数": {"stocks": 123}})
        self.assertEqual(self.run_item("pool")["status"], "延迟")

    def test_v3_non_rotation_month_does_not_require_daily_files(self):
        row = self.run_item("v3")
        self.assertEqual(row["status"], "正常", row)
        self.assertIn("非换股月", "".join(row["reasons"]))

    def test_v3_rotation_first_deadline_and_failure_guard(self):
        row = self.run_item("v3", "2026-11-01T08:20:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        row = self.run_item("v3", "2026-11-01T22:30:00+08:00")
        self.assertEqual(row["status"], "延迟", row)
        self.json("data/v3/失败_2026-11-01.json", {"asof": "2026-11-01", "incomplete": True, "counts": {"industry": 1300}})
        row = self.run_item("v3", "2026-11-01T22:30:00+08:00")
        self.assertEqual(row["status"], "失败", row)
        self.assertIn("失败保护", "".join(row["reasons"]))

    def test_v3_valid_period_not_daily_date(self):
        self.outputs("v3", "2026-11-01")
        obj = {"asof": "2026-11-01", "generated": "2026-11-01 12:20", "picks": [], "incomplete": False, "fetch_fail": 0}
        self.json("data/v3/latest.json", obj)
        self.json("data/v3/2026-11-01.json", obj)
        row = self.run_item("v3", "2026-11-03T22:30:00+08:00")
        self.assertEqual(row["status"], "正常", row)
        self.assertEqual(row["expected"]["data_date"], "2026-11-01")

    def test_saturday_rotation_is_checked_separately_from_trade_date(self):
        self.hangqing("2026-10-23")
        row = self.run_item("hangqing", "2026-10-24T22:30:00+08:00")
        self.assertEqual(row["status"], "延迟", row)
        self.assertIn("2026-10-24T10:17", row["expected"]["run_after"])
        report = (self.root / "logs/行情库_最新.md").read_text()
        self.put("logs/行情库_最新.md", report.replace("2026-10-23 18:00", "2026-10-24 11:30"))
        self.assertEqual(self.run_item("hangqing", "2026-10-24T22:30:00+08:00")["status"], "正常")

    def test_full_json_and_chinese_report_written_even_when_outputs_missing(self):
        status = health.main(["--root", str(self.root), "--now", "2026-10-28T22:30:00+08:00", "--json-out", "logs/test/health.json"])
        self.assertEqual(status, 0)
        result = json.loads((self.root / "logs/test/health.json").read_text())
        self.assertEqual(len(result["items"]), 10)
        for row in result["items"]:
            self.assertTrue({"id", "name", "status", "expected", "actual", "reasons"} <= row.keys())
            self.assertIn(row["status"], health.STATUSES)
        report = (self.root / "logs/健康检查_最新.md").read_text()
        self.assertIn("北京时间", report)
        self.assertIn("| 项目 | 状态 |", report)

    def test_malformed_manifest_still_generates_machine_readable_failure(self):
        self.put("system/运行清单.json", "{")
        result = health.main(["--root", str(self.root), "--now", "2026-10-28T22:30:00+08:00", "--json-out", "health.json"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads((self.root / "health.json").read_text())["items"][0]["status"], "失败")

    def test_future_artifact_timestamp_fails(self):
        self.news("2026-10-29 08:00")
        self.assertEqual(self.run_item("news")["status"], "失败")


if __name__ == "__main__":
    unittest.main()
