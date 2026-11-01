"""T5 每周市场全景：用临时小仓库离线测试，不读真实数据、不联网。"""
import csv
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import panorama  # noqa: E402
import health_check as health  # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))
# 2026-10-26 周一 ~ 10-30 周五；10-23 是上周五
DAYS = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_csv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def judgment(id_, date, module, scope, direction, review, status="待验证", result=None, check=None, auto=False):
    r = {"id": id_, "date": date, "module": module, "scope": {"type": "产业", "id": scope, "name": scope},
         "horizon": "周", "claim": f"{scope}的测试判断", "direction": direction, "confidence": "中",
         "evidence": [{"text": "t", "source": "t", "level": "一级"}], "verify": "v", "falsify": ["f"],
         "review_date": review, "status": status}
    if result:
        r["result"] = result
    if check:
        r["check"], r["auto"], r["confidence"] = check, True, "低"
    return r


class Fixture:
    def __init__(self, root: Path, market_days=DAYS, flow_days=DAYS):
        self.root = root
        write_csv(root / "data/行情库/指数/sh000300.csv", ["date", "close", "factor"], [[d, 1, 1] for d in DAYS])
        write(root / "system/交易所休市.json", json.dumps({"schema_version": 1, "coverage_start": "2026-01-01",
              "coverage_end": "2026-12-31", "closed_dates": ["2026-10-01"], "holiday_ranges": []}))
        temps = [32.0, 32.4, 30.8, 29.2, 28.2, 26.2, 25.1]
        write_csv(root / "data/社区情绪/温度计.csv", ["date", "温度", "成交额分位", "杠杆分位", "赚钱效应分位"],
                  [[d, t, 50, 4, 20] for d, t in zip(DAYS, temps)])
        write(root / "data/社区情绪/周报/2026-10-23.md", "散户情绪周报 2026-10-23｜温度 31→32（偏冷）｜阶段：愤怒→讨价还价（上周 无）\n")
        for i, d in enumerate(market_days):
            c = "攻" if d == "2026-10-28" else "守"
            write(root / f"data/市场状态/2026-10/{d}.json", json.dumps({"版本": "云端v1", "数据日期": d, "市场": {
                "结论": c, "冷热": "偏冷", "总分": -4, "沪深300": {"chg": -1.0 + i * 0.1}, "成交额": 15000 + i,
                "涨停": 40 + i, "跌停": 20, "依据": [f"{d} 依据行"]}, "数据告警": []}, ensure_ascii=False))
        rows = []
        for d in flow_days:
            for j, (code, name) in enumerate([("801010", "农林牧渔"), ("801080", "电子"), ("801780", "银行"),
                                               ("801150", "医药生物"), ("801730", "电力设备"), ("801770", "通信")]):
                rows.append([d, code, name, 0.05 + j / 100, (j - 2.5) / 1000, 50 + j, 0.01 * j])
        # 同日修订：电子在 10-30 追加一条，取最后一条
        rows.append(["2026-10-30", "801080", "电子", 0.30, 0.0123, 77, 0.02])
        write_csv(root / "data/资金轮动/日表.csv", ["date", "code", "name", "占比_31", "占比5日变化_31", "占比一年分位_31", "相对沪深300_5"], rows)
        write(root / "data/资金轮动/最新.json", json.dumps({"data_date": flow_days[-1]}))
        stage = {"801150": "拥挤", "801780": "确认", "801730": "启动"}
        write_csv(root / "data/主线/阶段日表_31.csv", ["date", "code", "行业", "阶段", "参数版本", "记录时间"],
                  [[d, c, n, stage.get(c, "无") if d == "2026-10-30" else "无", "T15草案v0", "x"]
                   for d in DAYS for c, n in [("801150", "医药生物"), ("801780", "银行"), ("801730", "电力设备"), ("801010", "农林牧渔")]])
        write_csv(root / "data/主线/阶段事件_31.csv", ["date", "code", "from", "to", "gate", "里程碑", "上位类型"], [
            ["2026-10-20", "801150", "无", "启动", "守", "启动", "共振"],
            ["2026-10-30", "801150", "启动", "拥挤", "守", "拥挤", ""],
            ["2026-10-27", "801780", "启动", "确认", "守", "确认", ""],
            ["2026-10-29", "801730", "无", "启动", "守", "启动", "未明"]])
        write(root / "data/主线/阶段_最新.json", json.dumps({"数据日期": "2026-10-30"}))
        write(root / "logs/自动复核_最新.json", json.dumps({"data_date": "2026-10-30"}))
        write(root / "graph/打分_最新.json", json.dumps({"as_of": "2026-10-27", "companies": [
            {"code": "002028", "name": "思源电气", "votes": 3, "families": [{"family": "AI", "vote": 1}, {"family": "GRID", "vote": 1}],
             "weighted_score": 8.5, "pricing": {"drawdown_52w": 0.55}, "pool": {"plan_status": "接近"}, "cross_candidate": True},
            {"code": "600941", "name": "中国移动", "votes": 2, "families": [], "cross_candidate": False}]}, ensure_ascii=False))
        js = [
            judgment("J-2026-10-22-001", "2026-10-22", "产业推演", "AI算力链", "看多", "2026-10-30"),
            judgment("J-2026-10-22-002", "2026-10-22", "驱动图谱", "AI算力链", "看空", "2026-12-30"),
            judgment("J-2026-10-22-003", "2026-10-22", "资金轮动", "银行", "看多", "2026-10-27", status="证实",
                     result={"date": "2026-10-28", "status": "证实", "note": "超额 +1.2%", "evidence": ["x"]},
                     check={"type": "指数涨跌"}),
            judgment("J-2026-10-22-004", "2026-10-22", "资金轮动", "酒", "看多", "2026-10-29", check={"type": "指数涨跌"}),
            judgment("J-2026-10-22-005", "2026-10-22", "产业推演", "存储", "看空", "2026-10-29", status="证伪",
                     result={"date": "2026-11-03", "status": "证伪", "note": "下周才复核", "evidence": ["x"]}),
            judgment("J-2026-11-02-001", "2026-11-02", "产业推演", "下周写的", "看多", "2026-11-10"),
        ]
        write(root / "judgments/2026-10.jsonl", "\n".join(json.dumps(j, ensure_ascii=False) for j in js) + "\n")


class PanoramaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        Fixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, day="2026-10-28"):
        w = panorama.Week(self.root, dt.date.fromisoformat(day))
        return panorama.build(self.root, w, dt.datetime(2026, 10, 31, 9, tzinfo=BJ))

    def test_all_sections_in_fixed_order_and_disclaimer(self):
        text, meta = self.build()
        titles = [l[3:] for l in text.splitlines() if l.startswith("## ")]
        self.assertEqual(titles, ["1. 散户情绪", "2. 大盘攻守", "3. 两层对照", "4. 资金在追什么、在撤什么", "5. 海外市场一周涨跌",
                                  "6. 主线雷达", "7. 交叉候选", "8. 冲突清单", "9. 到期复核", "10. 命中率", "11. 本周证实/证伪"])
        self.assertTrue(text.rstrip().endswith("不构成投资建议。"))
        self.assertIn("暂无数据，原因：T3", text)
        self.assertEqual(list(meta["缺数据栏目"]), ["5. 海外市场一周涨跌"])

    def test_numbers_match_sources(self):
        text, meta = self.build()
        self.assertIn("情绪温度 25（偏冷）｜阶段：愤怒→讨价还价｜温度 2026-10-30，阶段判读 2026-10-23", text)
        self.assertIn("阶段沿用 2026-10-23 的判读", text)
        self.assertIn("|2026-10-28|攻|", text)
        self.assertIn("2026-10-28：守→攻；2026-10-29：攻→守", text)
        # 电子取当日最后一条修订：占比 30.00%、+1.23 个百分点
        self.assertIn("|电子|30.00%|+1.23个百分点|", text)
        self.assertEqual(meta["栏目"]["4. 资金在追什么、在撤什么"]["在追"][0], "电子")
        self.assertIn("**拥挤**（1）：医药生物（自 10-30，上位类型 共振）", text)
        self.assertIn("**确认**（1）：银行", text)
        self.assertEqual(meta["栏目"]["3. 两层对照"]["阶段分布"]["确认"], 1)
        self.assertIn("|002028|思源电气|3|AI、GRID|8.50|55.00%|接近|", text)
        self.assertNotIn("中国移动", text)

    def test_judgments_as_of_week_end(self):
        text, meta = self.build()
        due = meta["栏目"]["9. 到期复核"]
        # 本周到期：001(10-30) 003(10-27) 004(10-29) 005(10-29)；005 的复核发生在下周，按本周日仍是待验证
        self.assertEqual(due["本周到期"], 4)
        self.assertIn("J-2026-10-22-001 [产业推演] AI算力链", text)
        self.assertIn("需人工复核：【待判读】", text)
        self.assertIn("程序待判", text)
        self.assertNotIn("下周才复核", text)
        self.assertNotIn("下周写的", text)
        self.assertEqual(meta["栏目"]["11. 本周证实/证伪"]["证实"], 1)
        self.assertIn("|资金轮动|程序|低|1|0|0|100%|", text)
        self.assertEqual(meta["栏目"]["10. 命中率"]["总数"], 5)

    def test_conflicts_listed_not_judged(self):
        text, _ = self.build()
        sec = text.split("## 8. 冲突清单", 1)[1].split("## 9.", 1)[0]
        self.assertIn("同一对象方向相反：AI算力链", sec)
        self.assertIn("大盘为守，但产业推演有看多判断：J-2026-10-22-001", sec)
        self.assertIn("大盘为守，但有方向在确认阶段（银行）", sec)
        self.assertNotIn("没有任何方向处在确认", sec)
        self.assertIn("只列出，不裁定", sec)

    def test_missing_sources_degrade_without_crash(self):
        for p in ["data/资金轮动/日表.csv", "data/主线/阶段日表_31.csv", "graph/打分_最新.json"]:
            (self.root / p).unlink()
        text, meta = self.build()
        self.assertIn("4. 资金在追什么、在撤什么", meta["缺数据栏目"])
        self.assertIn("6. 主线雷达", meta["缺数据栏目"])
        self.assertIn("3. 两层对照", meta["缺数据栏目"])
        self.assertEqual(text.count("暂无数据，原因："), 5)

    def test_previous_week_and_late_graph(self):
        text, meta = self.build("2026-10-23")
        self.assertIn("7. 交叉候选", meta["缺数据栏目"])
        self.assertIn("晚于本周", meta["缺数据栏目"]["7. 交叉候选"])
        self.assertIn("|2026-10-19|无存档|", text)

    def test_auto_waits_then_generates_once_and_never_overwrites(self):
        now = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=BJ)
        target = self.root / "panorama/2026-10-30.md"
        self.assertEqual(panorama.auto(self.root, now("2026-10-29T20:00:00")), 0)   # 还没到周五
        self.assertFalse(target.exists())
        write(self.root / "data/主线/阶段_最新.json", json.dumps({"数据日期": "2026-10-29"}))
        panorama.auto(self.root, now("2026-10-30T17:40:00"))                       # 上游未齐，未到截止
        self.assertFalse(target.exists())
        panorama.auto(self.root, now("2026-10-31T12:20:00"))                       # 过了周六截止，照样生成
        self.assertTrue(target.exists())
        log = json.loads((self.root / "logs/全景_最新.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "正常")
        self.assertIn("主线阶段", log["notes"][0])
        write(target, "Claude 已补写")
        panorama.auto(self.root, now("2026-10-31T20:00:00"))
        self.assertEqual(target.read_text(encoding="utf-8"), "Claude 已补写")
        with self.subTest("手动不加--force不覆盖"):
            code = panorama.main(["--root", str(self.root), "--week", "2026-10-30", "--now", "2026-10-31T21:00:00"])
            self.assertEqual(code, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "Claude 已补写")

    def test_auto_generates_as_soon_as_ready(self):
        now = dt.datetime(2026, 10, 30, 23, 0, tzinfo=BJ)
        panorama.auto(self.root, now)
        self.assertTrue((self.root / "panorama/2026-10-30.md").exists())

    def test_closed_week_only_updates_log(self):
        cal = json.loads((self.root / "system/交易所休市.json").read_text())
        cal["closed_dates"] = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
        write(self.root / "system/交易所休市.json", json.dumps(cal))
        panorama.auto(self.root, dt.datetime(2026, 10, 2, 18, tzinfo=BJ))
        self.assertFalse((self.root / "panorama").exists())
        log = json.loads((self.root / "logs/全景_最新.json").read_text(encoding="utf-8"))
        self.assertIn("全周休市", log["notes"][0])
        self.assertIsNone(log["file"])

    def test_failure_writes_log_and_keeps_last_success(self):
        panorama.auto(self.root, dt.datetime(2026, 10, 30, 23, 0, tzinfo=BJ))
        first = json.loads((self.root / "logs/全景_最新.json").read_text(encoding="utf-8"))["last_success_at"]
        (self.root / "data/行情库/指数/sh000300.csv").unlink()
        code = panorama.main(["--root", str(self.root), "--auto", "--now", "2026-11-06T18:00:00"])
        self.assertEqual(code, 1)
        log = json.loads((self.root / "logs/全景_最新.json").read_text(encoding="utf-8"))
        self.assertEqual(log["status"], "失败")
        self.assertEqual(log["last_success_at"], first)

    def test_health_check_sees_weekly_item(self):
        manifest = json.loads((ROOT / "system/运行清单.json").read_text(encoding="utf-8"))
        item = next(i for i in manifest["items"] if i["id"] == "panorama")
        panorama.auto(self.root, dt.datetime(2026, 10, 30, 23, 0, tzinfo=BJ))
        cal = health.Calendar(self.root, manifest["calendar"])
        ok = health.check_item(self.root, item, health.timestamp("2026-11-06T22:30:00+08:00"), cal)
        self.assertEqual(ok["status"], "正常", ok)
        late = health.check_item(self.root, item, health.timestamp("2026-11-07T12:00:00+08:00"), cal)
        self.assertEqual(late["status"], "延迟", late)

    def test_issue_offline_does_not_record(self):
        panorama.auto(self.root, dt.datetime(2026, 10, 30, 23, 0, tzinfo=BJ))
        # 测试本身可能跑在 Actions 上（GITHUB_ACTIONS=true），这里显式模拟本地环境，确保不联网
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            self.assertEqual(panorama.issue(self.root, dt.datetime(2026, 10, 30, 23, 5, tzinfo=BJ)), 0)
        self.assertFalse((self.root / "logs/全景提醒记录.csv").exists())


class WorkflowTests(unittest.TestCase):
    def test_workflow_triggers_and_writer_rules(self):
        s = (ROOT / ".github/workflows/panorama.yml").read_text(encoding="utf-8")
        self.assertIn("'37 9 * * 5'", s)
        self.assertIn("'17 4 * * 6'", s)
        self.assertIn("workflows: ['主线信号更新']", s)
        self.assertIn("scripts/panorama.py --auto", s)
        self.assertIn("scripts/panorama.py --issue", s)
        # 周六兜底时刻与脚本截止一致（12:17 晚于 12:00）
        self.assertEqual((panorama.DEADLINE_WEEKDAY, panorama.DEADLINE_TIME), (5, dt.time(12, 0)))


if __name__ == "__main__":
    unittest.main()
