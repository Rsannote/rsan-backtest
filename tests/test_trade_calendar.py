"""使用 Actions 已保存的官方公告验证解析和安全失败。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("trade_calendar", ROOT / "scripts/update_trade_calendar.py")
calendar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calendar)


class CalendarNoticeTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / "logs/test/T2/日历探测/上交所_休市.txt").read_bytes()

    def test_official_notice_has_full_year_and_all_holidays(self):
        data = calendar.parse_notice(self.raw, 2026)
        self.assertEqual(len(data["holiday_ranges"]), 7)
        self.assertEqual(data["coverage_end"], "2026-12-31")
        self.assertIn("2026-10-01", data["closed_dates"])
        self.assertIn("2026-10-07", data["closed_dates"])
        self.assertNotIn("2026-10-08", data["closed_dates"])

    def test_wrong_year_and_missing_holiday_rejected(self):
        with self.assertRaises(ValueError):
            calendar.parse_notice(self.raw, 2027)
        with self.assertRaises(ValueError):
            calendar.parse_notice(self.raw.replace("元旦：".encode(), "某节：".encode()), 2026)

    def test_script_text_cannot_override_visible_notice(self):
        poison = '<script>2027年休市安排 元旦：1月1日（周五）休市 相关公告</script>'.encode()
        self.assertEqual(calendar.parse_notice(poison + self.raw, 2026)["coverage_start"], "2026-01-01")

    def test_invalid_offline_source_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "system/交易所休市.json"
            path.parent.mkdir()
            path.write_text("保留旧文件", encoding="utf-8")
            bad = root / "bad.html"
            bad.write_text("无法解析的响应", encoding="utf-8")
            with patch("sys.argv", ["calendar", "--root", tmp, "--source", str(bad), "--year", "2026"]):
                self.assertEqual(calendar.main(), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "保留旧文件")


if __name__ == "__main__":
    unittest.main()
