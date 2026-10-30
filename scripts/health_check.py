#!/usr/bin/env python3
"""只读检查定时产出；不联网、不补抓、不修改 data。所有时间为北京时间。

交易日：沪深300 CSV 首末日期内按 date 成员判断；周末闭市；范围外
只接受交易所休市表覆盖，缺覆盖报告“无法确认”。休市表 schema：
{coverage_start, coverage_end, closed_dates: [YYYY-MM-DD], sources: [...]}。

check_mode缺省保留原时段逻辑；previous_trading_day在12:00前只核对严格
早于今天的上一交易日，之后按计划和清单宽限；freshness只核对最近成功
产出距今的分钟数，不要求当天日期，也不把失败运行时间当成功刷新。

--now 可注入北京时间；--json-out 给告警客户端消费。异常状态不会令本脚本
非零退出（报告生成成功为 0）；清单损坏仍生成失败报告并返回 2。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import re
from typing import Any

BJ = dt.timezone(dt.timedelta(hours=8))
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
STATUSES = ("正常", "延迟", "失败", "部分失败")


def timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"时间字段须为字符串：{value!r}")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=BJ) if parsed.tzinfo is None else parsed.astimezone(BJ)


def day(value: Any) -> dt.date:
    return dt.date.fromisoformat(str(value)[:10])


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV无表头：{path.name}")
        return list(reader)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def first_stamp(content: str) -> dt.datetime:
    found = STAMP.search(content)
    if not found:
        raise ValueError("缺少运行时间")
    return timestamp(found.group())


def csv_date(path: Path, column: str = "date") -> str:
    rows = read_csv(path)
    if not rows:
        raise ValueError(f"CSV没有数据：{path.name}")
    return max(day(r[column]) for r in rows).isoformat()


class Calendar:
    def __init__(self, root: Path, config: dict[str, Any]):
        self.days: set[dt.date] = set()
        self.closed: set[dt.date] = set()
        self.start = self.end = None
        self.errors: list[str] = []
        self.unknown: set[str] = set()
        try:
            self.days = {day(r["date"]) for r in read_csv(root / config["history"])}
            if not self.days:
                raise ValueError("交易日历为空")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.errors.append(f"历史日历不可读：{exc}")
        self.closures_path = root / config["closures"]
        self.closures = None
        try:
            obj = read_json(self.closures_path)
            self.start, self.end = day(obj["coverage_start"]), day(obj["coverage_end"])
            if self.start > self.end or not isinstance(obj["closed_dates"], list):
                raise ValueError("覆盖区间或closed_dates无效")
            self.closed = {day(d) for d in obj["closed_dates"]}
            if any(not self.start <= d <= self.end for d in self.closed):
                raise ValueError("休市日期超出覆盖区间")
            self.closures = obj
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.start = self.end = None
            self.errors.append(f"交易所日历不可读：{exc}")

    def is_open(self, date: dt.date) -> bool | None:
        if date.weekday() >= 5:
            return False
        if self.days and min(self.days) <= date <= max(self.days):
            return date in self.days
        if self.start and self.start <= date <= self.end:
            return date not in self.closed
        self.unknown.add(date.isoformat())
        return None


def scheduled(date: dt.date, time: str) -> dt.datetime:
    return timestamp(f"{date.isoformat()}T{time}:00")


def expectation(item: dict[str, Any], now: dt.datetime, cal: Calendar) -> dict[str, Any]:
    kind = item["kind"]
    mode = item.get("check_mode")
    if mode not in (None, "previous_trading_day", "freshness"):
        raise ValueError(f"未知check_mode：{mode}")
    if kind in ("calendar", "self"):
        return {"data_date": now.date().isoformat()}
    if mode == "freshness":
        minutes = item["max_age_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 < minutes < float("inf"):
            raise ValueError("max_age_minutes须为有限正数")
        return {"check_mode": mode, "max_age_minutes": minutes,
                "cutoff": (now - dt.timedelta(minutes=minutes)).isoformat()}
    if kind == "v3":
        if now.month not in item["months"]:
            return {"skipped": "非换股月，无本月更新要求"}
        date = now.date().replace(day=1)
        start = scheduled(date, item["times"][0])
        deadline = start + dt.timedelta(minutes=item["grace_minutes"])
        if now < deadline:
            return {"skipped": f"本期尚未到截止 {deadline.isoformat()}"}
        return {"data_date": date.isoformat(), "scheduled_at": start.isoformat(), "deadline": deadline.isoformat()}
    for offset in range(370):
        date = now.date() - dt.timedelta(days=offset)
        morning_previous = mode == "previous_trading_day" and now.hour < 12
        if morning_previous and offset == 0:
            continue
        for time in sorted(item["times"], reverse=True):
            start = scheduled(date, time)
            deadline = start + dt.timedelta(minutes=item.get("grace_minutes", 0))
            if deadline > now and not morning_previous:
                continue
            if item.get("trading_days_only") or mode == "previous_trading_day":
                opened = cal.is_open(date)
                if opened is None:
                    return {"unknown_date": date.isoformat(), "error": f"无法确认 {date} 是否交易日：历史日历未覆盖，交易所休市表也无有效覆盖"}
                if not opened:
                    continue
            result = {"data_date": date.isoformat(), "scheduled_at": start.isoformat(), "deadline": deadline.isoformat()}
            if mode:
                result["check_mode"] = mode
            extra = item.get("extra_schedule")
            if extra:
                for back in range(7):
                    run_date = now.date() - dt.timedelta(days=back)
                    rotation = scheduled(run_date, extra["time"])
                    cutoff = rotation + dt.timedelta(minutes=extra["grace_minutes"])
                    if run_date.weekday() in extra["weekdays"] and cutoff <= now:
                        # 周六仅当比最近日更任务晚，才另要求轮换全量日志。
                        if rotation > start:
                            result["run_after"] = rotation.isoformat()
                        break
            return result
    raise ValueError("过去370天没有可判定的运行时段")


def success_timestamps(value: Any, field: str | None = None) -> list[dt.datetime]:
    """提取明确的成功字段；字段路径支持*，不把普通运行时刻当成功。"""
    if field is not None:
        parts = field.split(".")
        values = [value]
        for part in parts:
            selected = []
            for current in values:
                if isinstance(current, dict):
                    selected.extend(current.values() if part == "*" else [current[part]] if part in current else [])
                elif isinstance(current, list) and part == "*":
                    selected.extend(current)
            values = selected
        return [timestamp(v) for v in values if v is not None]
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("last_success_at", "最近成功", "最近成功运行", "最近成功时间", "最近抓取") and child:
                result.append(timestamp(child))
            elif isinstance(child, (dict, list)):
                result.extend(success_timestamps(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(success_timestamps(child))
    return result


def configured_success(root: Path, item: dict[str, Any]) -> list[dt.datetime]:
    paths = item.get("freshness_success_paths", [])
    if not isinstance(paths, list):
        raise ValueError("freshness_success_paths须为数组")
    result = []
    for entry in paths:
        if isinstance(entry, str):
            path, field = entry, None
        elif isinstance(entry, dict):
            path, field = entry["path"], entry.get("field")
            if field is not None and not isinstance(field, str):
                raise ValueError("成功时间字段路径须为字符串")
        else:
            raise ValueError("成功时间配置须为文件路径或path/field对象")
        target = root / path
        if target.is_file():
            result.extend(success_timestamps(read_json(target), field))
    return result


def fail_sections(content: str) -> list[str]:
    """只读失败/超时小节，避免把历史跳空提醒或补抓成功误当失败。"""
    result: list[str] = []
    active = False
    for line in content.splitlines():
        if line.startswith("## "):
            active = bool(re.match(r"##\s*(失败|超时未轮到)(?:\s|（|$)", line))
        elif active and line.startswith("- "):
            result.append(line[2:])
    return result


def load_actual(root: Path, item: dict[str, Any], expected: dict[str, Any]) -> tuple[dict[str, Any], list[str], int]:
    """返回实际日期、既有失败原因、成功子项数；未解释的旧数据仅判延迟。"""
    kind = item["kind"]
    issues: list[str] = []
    actual: dict[str, Any] = {}
    success = 1
    successful_runs: list[dt.datetime] = []
    if kind == "hangqing":
        rows = read_csv(root / "data/行情库/状态.csv")
        if not rows:
            raise ValueError("行情状态表为空")
        dates = [day(r["last"]).isoformat() for r in rows]
        actual["data_date"] = max(dates)
        actual["updated_at"] = max(timestamp(r["updated"]) for r in rows).isoformat()
        report = text(root / "logs/行情库_最新.md")
        actual["run_at"] = first_stamp(report).isoformat()
        issues = fail_sections(report)
        count = re.search(r"成功\s*(\d+)", report)
        success = int(count.group(1)) if count else len(rows)
        summary = re.search(r"失败\s*(\d+).*?超时未轮到\s*(\d+)", report)
        if summary:
            for label, number in zip(("失败", "超时未轮到"), summary.groups()):
                if int(number) and not issues:
                    issues.append(f"日志记载{label} {number} 个，但没有逐项列表")
        # 状态表可能保留停牌日期，不能只因某标的last旧就断言抓取失败。
        # 仍展示具体日期和名称，失败证据由已有日志决定。
        actual["stale_symbols"] = [{"code": r["code"], "name": r["name"], "date": r["last"]} for r in rows if r["last"] < expected["data_date"]]
        actual["failed"] = issues
    elif kind == "market_state":
        obj = read_json(root / "data/市场状态/最新.json")
        actual = {"data_date": day(obj["数据日期"]).isoformat(), "updated_at": timestamp(obj["生成时间"]).isoformat()}
        warnings = obj.get("数据告警", [])
        if not isinstance(warnings, list):
            raise ValueError("数据告警须为数组")
        issues.extend(str(w) for w in warnings)
    elif kind == "judgment_report":
        obj = read_json(root / item["outputs"][0])
        actual = {"data_date": day(obj["data_date"]).isoformat(), "updated_at": timestamp(obj["generated_at"]).isoformat()}
        if obj.get("status") not in ("正常", "失败") or not isinstance(obj.get("errors"), list):
            raise ValueError("程序判断报告缺少有效status/errors")
        issues.extend(str(error) for error in obj["errors"])
        if obj["status"] == "失败":
            success = 0
            if not issues:
                issues.append("程序判断报告标记失败")
        # 草案未启用不算失败；到期后固定窗口缺行情需暴露，不能顺延抹掉。
        issues.extend(str(item) for item in obj.get("pending", []))
    elif kind == "news":
        obj = read_json(root / "data/新闻/状态.json")
        run = timestamp(obj["最近运行"])
        actual = {"data_date": run.date().isoformat(), "updated_at": run.isoformat()}
        sources = obj["来源"]
        if not isinstance(sources, dict) or not sources:
            raise ValueError("新闻来源状态为空或格式错误")
        success = sum(v["ok"] is True for v in sources.values())
        for name, source in sources.items():
            if not isinstance(source.get("ok"), bool):
                raise ValueError(f"{name} 缺少有效ok布尔值")
            if not source["ok"]:
                issues.append(f"{name}：{source.get('err', 'ok=false')}")
        if success:
            successful_runs.append(run)
        # 失败运行仍会覆盖“最近运行”，只有明确的历史成功字段才能回退。
        if item.get("check_mode") == "freshness":
            successful_runs.extend(success_timestamps(obj))
        actual["slot_evidence"] = "状态文件只保留最近一次运行；不据此声称历史四次均成功"
    elif kind == "community":
        folder = root / "data/社区情绪" / expected["data_date"]
        report = text(folder / "运行记录.txt")
        run = first_stamp(report)
        source_date = expected["data_date"] if item.get("check_mode") == "previous_trading_day" else run.date().isoformat()
        actual = {"data_date": source_date, "updated_at": run.isoformat()}
        success_line = re.search(r"^成功：(.*)$", report, re.M)
        success = len(success_line.group(1).split("、")) if success_line and success_line.group(1) != "无" else 0
        block = re.search(r"^失败：\s*\n(.*?)(?=\n讨论摘录|\Z)", report, re.M | re.S)
        if block:
            issues += [line for line in block.group(1).splitlines() if line.strip() and line.strip() != "无"]
        block = re.search(r"讨论摘录失败：\s*\n(.*)", report, re.S)
        if block:
            issues += [line for line in block.group(1).splitlines() if line.strip() and line.strip() != "无"]
        if success == 0 and not issues:
            issues.append("本次运行没有任何成功的数据源")
    elif kind == "sentiment_index":
        obj = read_json(root / "data/社区情绪/触发.json")
        actual = {"data_date": day(obj["date"]).isoformat(), "temperature_date": day(obj["温度日期"]).isoformat(), "temperature_csv_date": csv_date(root / "data/社区情绪/温度计.csv"), "text_csv_date": csv_date(root / "data/社区情绪/文本指标.csv")}
        for key in ("temperature_date", "temperature_csv_date", "text_csv_date"):
            if actual[key] < actual["data_date"]:
                issues.append(f"{key}仅到 {actual[key]}，触发文件已到 {actual['data_date']}")
    elif kind == "tophub":
        report = text(root / "data/信息源/tophub/运行记录.txt")
        blocks = list(re.finditer(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$", report, re.M))
        if not blocks:
            raise ValueError("热榜运行记录缺少时间标题")
        marker = blocks[-1]
        run = timestamp(marker.group(1))
        block = report[marker.end():]
        actual = {"data_date": run.date().isoformat(), "updated_at": run.isoformat()}
        success = 0
        for line in block.splitlines():
            columns = line.split("\t")
            if len(columns) >= 2 and (columns[1] == "失败" or columns[1] == "0条"):
                issues.append("：".join(columns))
            elif len(columns) >= 2 and re.fullmatch(r"[1-9]\d*条", columns[1]):
                success += 1
        state = read_json(root / "data/信息源/tophub/状态.json")
        if not isinstance(state, dict):
            raise ValueError("热榜状态格式错误")
        # 最近抓取只在该榜单成功时更新；全部失败也能由运行日志发现。
        actual["boards"] = len(state)
        actual["successful_boards"] = success
        if not success and not issues:
            issues.append("本次运行没有任何榜单条目")
        if item.get("check_mode") == "freshness":
            # 日志本身保留多个运行块；只选确有成功榜单条目的时间。
            for index, candidate in enumerate(blocks):
                stop = blocks[index + 1].start() if index + 1 < len(blocks) else len(report)
                if re.search(r"^[^\n\t]+\t[1-9]\d*条(?:\t|$)", report[candidate.end():stop], re.M):
                    successful_runs.append(timestamp(candidate.group(1)))
            # 旧状态能补足被裁剪的运行记录，但失败块同一时刻的状态不算刷新。
            successful_runs.extend(t for t in success_timestamps(state) if success or t < run)
    elif kind == "pool":
        obj = read_json(root / "data/股票池/状态.json")
        actual = {"data_date": day(obj["数据日期"]).isoformat(), "updated_at": timestamp(obj["最近检查"]).isoformat()}
        if not isinstance(obj.get("文档数"), dict) or obj["文档数"].get("stocks", 0) <= 0:
            issues.append("股票池镜像缺少stocks文档或为空")
            success = 0
    elif kind == "v3":
        target = root / "data/v3" / (expected["data_date"] + ".json")
        obj = read_json(target)
        latest = read_json(root / "data/v3/latest.json")
        actual = {"data_date": day(obj["asof"]).isoformat(), "latest_date": day(latest["asof"]).isoformat(), "updated_at": timestamp(obj["generated"]).isoformat()}
        if obj.get("incomplete") or latest.get("incomplete"):
            issues.append("正式名单标记未完成，不可用")
            success = 0
        if obj.get("fetch_fail", 0):
            issues.append(f"取数失败 {obj['fetch_fail']} 次，见 logs/v3_最新.md")
        if not isinstance(obj.get("picks"), list):
            raise ValueError("正式名单缺少picks数组")
        if actual["latest_date"] != actual["data_date"]:
            issues.append(f"latest.json换股期 {actual['latest_date']} 与正式文件不一致")
    else:
        # 新任务可用kind=json_date和字段配置接入，不必另写抓取逻辑。
        obj = read_json(root / item["outputs"][0])
        actual["data_date"] = day(obj[item.get("date_field", "date")]).isoformat()
    if item.get("check_mode") == "freshness":
        successful_runs.extend(configured_success(root, item))
        if success == 0:
            failed_run = timestamp(actual["updated_at"])
            successful_runs = [value for value in successful_runs if value < failed_run]
        actual["last_success_at"] = max(successful_runs).isoformat() if successful_runs else None
    return actual, issues, success


def check_item(root: Path, item: dict[str, Any], now: dt.datetime, cal: Calendar) -> dict[str, Any]:
    row = {"id": item["id"], "name": item["name"], "status": "正常", "expected": {}, "actual": {}, "reasons": []}
    try:
        expected = expectation(item, now, cal)
        row["expected"] = expected
        if expected.get("error"):
            row["status"], row["reasons"] = "失败", [expected["error"]]
            return row
        if expected.get("skipped"):
            row["reasons"].append(expected["skipped"])
            return row
        if item["kind"] == "self":
            row["actual"] = {"updated_at": now.isoformat(), "data_date": now.date().isoformat()}
            row["reasons"] = ["本次检查成功生成报告；工作流整体失败由Actions失败告警处理"]
            return row
        if item["kind"] == "calendar":
            row["actual"] = {"coverage_start": cal.start.isoformat() if cal.start else None, "coverage_end": cal.end.isoformat() if cal.end else None, "unknown_dates": sorted(cal.unknown)}
            if cal.unknown:
                row["status"] = "失败"
                row["reasons"] = ["交易所日历未覆盖待检查工作日：" + "、".join(sorted(cal.unknown))] + cal.errors
            elif not cal.closures:
                row["reasons"] = ["可选交易所日历不可用；本次待检查日期均可由历史日历或周末判定"]
            else:
                row["reasons"] = ["已读取交易所日历覆盖；历史行情首末日期内仍按成员判定"]
            return row
        output_date = expected.get("data_date", now.date().isoformat())
        read_expected = dict(expected, data_date=output_date)
        if item.get("check_mode") == "previous_trading_day" and item["kind"] == "community":
            # 宽限只降低日期要求，不能让今天已经写出的失败躲在昨天目录后。
            base = root / "data/社区情绪"
            available = [p.name for p in base.iterdir() if p.is_dir() and DATE.fullmatch(p.name)
                         and p.name <= now.date().isoformat() and (p / "运行记录.txt").is_file()] if base.is_dir() else []
            if available:
                output_date = max(available)
                read_expected["data_date"] = output_date
        missing = [p.format(date=output_date) for p in item["outputs"] if not (root / p.format(date=output_date)).is_file()]
        if missing:
            # v3失败保护文件是比“缺正式名单”更具体的失败证据。
            failure = root / "data/v3" / ("失败_" + output_date + ".json")
            if item["kind"] == "v3" and failure.exists():
                obj = read_json(failure)
                row["actual"] = {"failure_file": str(failure.relative_to(root)), "incomplete": obj.get("incomplete"), "counts": obj.get("counts", {})}
                row["status"] = "失败"
                row["reasons"] = ["本期选股失败保护已触发，未写正式名单"]
            else:
                row["status"] = "延迟" if item["kind"] in ("community", "v3") else "失败"
                row["reasons"] = ["缺少产出：" + "、".join(missing)]
            # 缺其他产出时仍读已有失败证据，不被缺文件遮住。
            if item["kind"] == "hangqing" and (root / "logs/行情库_最新.md").exists():
                row["reasons"] += fail_sections(text(root / "logs/行情库_最新.md"))
            if item.get("check_mode") == "previous_trading_day" and item["kind"] == "community" and (root / "data/社区情绪" / output_date / "运行记录.txt").is_file():
                actual, failures, success = load_actual(root, item, read_expected)
                row["actual"] = actual
                row["reasons"] += failures
                if failures:
                    row["status"] = "部分失败" if success else "失败"
            return row
        actual, failures, success = load_actual(root, item, read_expected)
        row["actual"] = actual
        row["reasons"] += failures
        if any(value > now.date().isoformat() for key, value in actual.items() if key.endswith("_date") and isinstance(value, str)):
            raise ValueError("产出数据日期在检查时间之后")
        if actual.get("updated_at") and timestamp(actual["updated_at"]) > now:
            raise ValueError("产出运行时间在检查时间之后")
        if item.get("check_mode") == "freshness":
            last_success = timestamp(actual["last_success_at"]) if actual.get("last_success_at") else None
            if last_success and last_success > now:
                raise ValueError("最近成功时间在检查时间之后")
            stale = last_success is None or last_success < timestamp(expected["cutoff"])
            actual["age_minutes"] = round((now - last_success).total_seconds() / 60, 6) if last_success else None
        else:
            stale = actual["data_date"] < expected["data_date"]
            # 旧模式保持按slot核验；新交易日模式只要求目标交易日已覆盖。
            if item.get("check_mode") is None and not item.get("trading_days_only") and item["kind"] != "v3":
                stale = stale or timestamp(actual["updated_at"]) < timestamp(expected["scheduled_at"])
            if expected.get("run_after"):
                stale = stale or timestamp(actual["run_at"]) < timestamp(expected["run_after"])
        if failures:
            row["status"] = "部分失败" if success > 0 else "失败"
        elif stale:
            row["status"] = "延迟"
        if stale:
            if item.get("check_mode") == "freshness":
                if actual["last_success_at"] is None:
                    row["status"] = "失败"
                    row["reasons"].append("无法确认最近成功产出时间；失败运行不作为成功刷新")
                else:
                    row["reasons"].append(f"最近成功产出距今 {actual['age_minutes']:g} 分钟，超过阈值 {expected['max_age_minutes']} 分钟（最早应为 {expected['cutoff']}）")
            else:
                row["reasons"].append(f"已到截止 {expected['deadline']}；实际未覆盖预期数据日期/运行时段 {expected.get('run_after', expected['scheduled_at'])}")
        if not row["reasons"]:
            row["reasons"] = ([f"最近成功产出距今 {actual['age_minutes']:g} 分钟，未超过阈值 {expected['max_age_minutes']} 分钟"]
                              if item.get("check_mode") == "freshness" else ["产出已覆盖要求的交易日"]
                              if item.get("check_mode") == "previous_trading_day" else ["产出日期及最近已到截止的运行时段符合预期"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        row["status"] = "失败"
        row["reasons"].append(f"产出无法读取或格式错误：{exc}")
    return row


def load_manifest(root: Path) -> dict[str, Any]:
    """主清单保持兼容；独立任务片段只追加items，不覆盖日历或既有策略。"""
    manifest = read_json(root / "system/运行清单.json")
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("items"), list):
        raise ValueError("运行清单schema_version/items无效")
    for path in sorted((root / "system/运行清单.d").glob("*.json")):
        fragment = read_json(path)
        if set(fragment) != {"schema_version", "items"} or fragment["schema_version"] != 1 or not isinstance(fragment["items"], list):
            raise ValueError("独立运行清单片段格式无效：" + str(path))
        manifest["items"].extend(fragment["items"])
    if len({i["id"] for i in manifest["items"]}) != len(manifest["items"]):
        raise ValueError("运行清单id重复")
    return manifest


def check(root: Path, now: dt.datetime, *, manifest=None) -> dict[str, Any]:
    # 旧库调用（包括T11的历史延迟回放）保持原主清单口径；生产CLI显式传合并清单。
    manifest = read_json(root / "system/运行清单.json") if manifest is None else manifest
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("items"), list):
        raise ValueError("运行清单schema_version/items无效")
    if len({i["id"] for i in manifest["items"]}) != len(manifest["items"]):
        raise ValueError("运行清单id重复")
    cal = Calendar(root, manifest["calendar"])
    # 日历自检置后，汇总各任务实际需要判定却无法覆盖的日期。
    items = sorted(manifest["items"], key=lambda i: i["kind"] == "calendar")
    rows = [check_item(root, item, now, cal) for item in items]
    order = {item["id"]: i for i, item in enumerate(manifest["items"])}
    rows.sort(key=lambda row: order[row["id"]])
    return {"schema_version": 1, "checked_at": now.isoformat(), "timezone": "Asia/Shanghai", "items": rows}


def render(report: dict[str, Any]) -> str:
    def safe(value: Any) -> str:
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return value.replace("|", "\\|").replace("\n", " ")
    counts = "，".join(f"{status} {sum(row['status'] == status for row in report['items'])} 项" for status in STATUSES)
    lines = ["# 运行健康检查", "", f"检查时间：{report['checked_at']}（北京时间）", "", counts, "", "| 项目 | 状态 | 预期 | 实际 | 原因 |", "|---|---|---|---|---|"]
    for row in report["items"]:
        expected, actual = row["expected"], row["actual"]
        expectation_text = expected.get("skipped") or expected.get("error") or expected.get("data_date", "—")
        if expected.get("check_mode") == "freshness":
            expectation_text = f"最近成功产出不超过 {expected['max_age_minutes']} 分钟；下限 {expected['cutoff'][5:16].replace('T', ' ')}"
        if expected.get("deadline"):
            expectation_text += f"；截止 {expected['deadline'][5:16].replace('T', ' ')}"
        if expected.get("run_after"):
            expectation_text += f"；周六轮换 {expected['run_after'][5:16].replace('T', ' ')}"
        actual_text = actual.get("data_date", "—")
        if actual.get("updated_at"):
            actual_text += f"；运行 {actual['updated_at'][5:16].replace('T', ' ')}"
        if "last_success_at" in actual:
            actual_text += f"；最近成功 {actual['last_success_at'][5:16].replace('T', ' ') if actual['last_success_at'] else '无法确认'}"
        if actual.get("stale_symbols"):
            actual_text += f"；{len(actual['stale_symbols'])}只数据日期较早（详见JSON）"
        if "coverage_end" in actual:
            actual_text = f"覆盖 {actual.get('coverage_start') or '无'} 至 {actual.get('coverage_end') or '无'}"
        reasons = re.sub(r"https?://\S+", "〔请求地址略〕", "；".join(row["reasons"]))
        lines.append("| " + " | ".join(safe(v) for v in (row["name"], row["status"], expectation_text, actual_text, reasons)) + " |")
    lines += ["", "交易日规则：周末闭市；沪深300历史首末日期内按date成员判定；范围外仅使用交易所休市表的明确覆盖。缺覆盖写失败并注明无法确认，不把缺今日行情当休市。", "", "previous_trading_day：12:00前只要求严格早于今天的上一交易日；之后按计划时刻加清单宽限决定是否要求今天，周六轮换亦遵守清单截止。freshness：仅核验最近成功产出距今是否超过清单阈值，跨午夜不要求当天日期；失败运行不重置成功时间。未配置check_mode的旧项仍按最近已到截止slot检查。失败优先于延迟；部分成功且有既有失败证据列部分失败，原因同时保留延迟。检查只读产出，不补抓。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按运行清单检查产出并生成中文健康报告")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--now", help="可注入ISO时间；不带时区时按北京时间")
    parser.add_argument("--json-out", type=Path, help="机器可读报告路径，相对路径以root为准")
    parser.add_argument("--report", type=Path, default=Path("logs/健康检查_最新.md"))
    args = parser.parse_args(argv)
    now = timestamp(args.now) if args.now else dt.datetime.now(BJ)
    root = args.root.resolve()
    code = 0
    try:
        report = check(root, now, manifest=load_manifest(root))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code = 2
        report = {"schema_version": 1, "checked_at": now.isoformat(), "timezone": "Asia/Shanghai", "items": [{"id": "manifest", "name": "运行清单", "status": "失败", "expected": {}, "actual": {}, "reasons": [f"清单无法读取或格式错误：{exc}"]}]}
    output = args.report if args.report.is_absolute() else root / args.report
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(report), encoding="utf-8")
    if args.json_out:
        output = args.json_out if args.json_out.is_absolute() else root / args.json_out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"健康检查 {report['checked_at']}：" + "，".join(f"{s}{sum(i['status'] == s for i in report['items'])}项" for s in STATUSES))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
