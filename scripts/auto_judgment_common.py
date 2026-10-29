"""程序判断共用的离线交易日窗口、格式验证与原子档案写入。"""
from __future__ import annotations
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import tempfile
from check_judgments import check_record, check_condition

BJ = dt.timezone(dt.timedelta(hours=8))


def now_bj():
    return dt.datetime.now(BJ)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_report(directory, name, report):
    directory = Path(directory)
    atomic_text(directory / (name + ".json"), json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# " + report["title"], "", "北京时间：" + report["generated_at"],
             "数据日期：" + report["data_date"], "", "状态：" + report["status"], ""]
    for key in ("summary", "skipped", "errors", "manual", "pending"):
        if key in report:
            lines += ["## " + {"summary":"指标", "skipped":"未启用/跳过", "errors":"失败", "manual":"待人工复核", "pending":"等待数据/到期"}[key], ""]
            values = report[key]
            lines.extend("- " + (x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)) for x in values)
            if not values:
                lines.append("- 无")
            lines.append("")
    atomic_text(directory / (name + ".md"), "\n".join(lines) + "\n")


class TradingCalendar:
    def __init__(self, root):
        self.root = Path(root)
        self.days = sorted({dt.date.fromisoformat(r["date"]) for r in read_csv(self.root / "data/行情库/指数/sh000300.csv")})
        if not self.days:
            raise ValueError("沪深300交易日历为空")
        path = self.root / "system/交易所休市.json"
        self.official = read_json(path) if path.exists() else None
        self.closed = set(self.official["closed_dates"]) if self.official else set()

    def is_open(self, day):
        if day.weekday() >= 5:
            return False
        if self.official and self.official["coverage_start"] <= day.isoformat() <= self.official["coverage_end"]:
            return day.isoformat() not in self.closed
        if self.days[0] <= day <= self.days[-1]:
            return day in self.days
        raise ValueError(f"交易日历未覆盖{day}，不猜测节假日")

    def window(self, start, n):
        start = dt.date.fromisoformat(start) if isinstance(start, str) else start
        if not self.is_open(start):
            raise ValueError(f"起点{start}不是交易日")
        result = [start]
        current = start
        for _ in range(800):
            current += dt.timedelta(days=1)
            if self.is_open(current):
                result.append(current)
            if len(result) == n + 1:
                return result
        raise ValueError("窗口超出允许长度")

    def previous(self, day):
        current=dt.date.fromisoformat(day) if isinstance(day,str) else day
        for _ in range(370):
            current-=dt.timedelta(days=1)
            if self.is_open(current):
                return current
        raise ValueError("找不到上一交易日")


def prices(root, kind, code):
    category = "ETF" if kind == "行业ETF相对沪深300" else "指数"
    path = Path(root) / "data/行情库" / category / (code + ".csv")
    result = {}
    for row in read_csv(path):
        day = row["date"]
        dt.date.fromisoformat(day)
        if day in result:
            raise ValueError(f"{code}日期重复：{day}")
        result[day] = row
    return result, str(path.relative_to(root))


def evaluate(root, record, as_of, calendar=None):
    """不修改输入；严格校验完整固定窗口，未来或缺失返回待验证原因。"""
    condition = record["check"]
    errors = check_condition(condition)
    if errors:
        raise ValueError("；".join(errors))
    calendar = calendar or TradingCalendar(root)
    window = calendar.window(condition["from"], condition["to_trading_days"])
    target = window[-1].isoformat()
    if target != record["review_date"]:
        raise ValueError("review_date与固定交易日终点不一致")
    as_of = dt.date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if window[-1] > as_of:
        return {"pending": f"未到期，固定终点{target}", "target": target}
    returns, evidence, drawdown = [], [], None
    codes = [(condition["type"], condition["code"])]
    if condition["type"] in {"指数相对", "行业ETF相对沪深300"}:
        codes.append(("指数涨跌", condition["benchmark"]))
    for kind, code in codes:
        try:
            rows, path = prices(root, kind, code)
            missing = [d.isoformat() for d in window if d.isoformat() not in rows]
            if missing:
                return {"pending": f"{code}固定窗口缺日：" + "、".join(missing), "target": target}
            adjusted = []
            for day in window:
                row = rows[day.isoformat()]
                close, factor = float(row["close"]), float(row["factor"])
                if not all(math.isfinite(v) and v > 0 for v in (close, factor)):
                    raise ValueError(f"{code} {day} close/factor无效")
                adjusted.append(close * factor)
            returns.append(adjusted[-1] / adjusted[0] - 1)
            if condition["type"] == "指数最大回撤":
                drawdown = 1 - min(adjusted) / adjusted[0]
                description = f"起点收盘{adjusted[0]:g}、窗口最低收盘{min(adjusted):g}、起点最大回撤{drawdown:.8%}"
            else:
                description = f"收益{returns[-1]:+.8%}"
            evidence.append({"text": f"{code} {condition['from']}→{target} close×factor{description}", "source": path, "level": "一级"})
        except (OSError, KeyError, ValueError) as exc:
            return {"pending": str(exc), "target": target}
    actual = drawdown if drawdown is not None else returns[0] - (returns[1] if len(returns) == 2 else 0)
    op, value = condition["op"], condition["value"]
    hit = {">": actual > value, ">=": actual >= value, "<": actual < value, "<=": actual <= value}[op]
    status = "证实" if hit else "证伪"
    return {"status": status, "actual": actual, "target": target, "returns": returns,
            "result": {"date": as_of.isoformat(), "status": status,
                       "note": f"固定窗口{condition['from']}→{target}（{condition['to_trading_days']}交易日）；实际{actual:+.8%}；条件 {op} {value:+.8%}。close×factor，{'起点收盘至窗口最低收盘回撤，不是滚动高点回撤' if drawdown is not None else '端点收益差'}；缺日不顺延。",
                       "evidence": evidence}}


def load_documents(directory):
    documents, ids, keys = {}, {}, {}
    for path in sorted(Path(directory).glob("*.jsonl")):
        raw = path.read_bytes().decode("utf-8")
        documents[path] = raw.splitlines(keepends=True)
        for index, line in enumerate(documents[path]):
            if not line.strip():
                continue
            record = json.loads(line)
            errors = check_record(record, f"{path.name}:{index+1}")
            if errors:
                raise ValueError("；".join(errors))
            if record["id"] in ids:
                raise ValueError("重复判断ID：" + record["id"])
            ids[record["id"]] = (path, index, record)
            if record.get("auto_key"):
                if record["auto_key"] in keys:
                    raise ValueError("重复程序事件auto_key")
                keys[record["auto_key"]] = record
    return documents, ids, keys


def persist(directory, additions=(), reviews=None):
    """先验证整批更改；每文件原子替换。未复核行逐字保留，跨文件失败可幂等续跑。"""
    directory = Path(directory)
    documents, ids, keys = load_documents(directory)
    changed, inserted = set(), 0
    sequences={}
    for identifier in ids:
        sequences[identifier[2:12]]=max(sequences.get(identifier[2:12],0),int(identifier[-3:]))
    for record in additions:
        errors = check_record(record, "写入前")
        if errors:
            raise ValueError("；".join(errors))
        old = keys.get(record["auto_key"])
        if old:
            # 午夜后重跑保留首条date/id，比较全部不可变事件内容。
            omit = {"id", "date", "status", "result"}
            if {k:v for k,v in old.items() if k not in omit} != {k:v for k,v in record.items() if k not in omit}:
                raise ValueError("同auto_key内容冲突：" + record["auto_key"])
            continue
        serial=sequences.get(record["date"],0)+1
        if serial>999:
            raise ValueError("当天判断超过三位序号容量，不覆盖已有ID")
        sequences[record["date"]]=serial
        record=dict(record,id=f"J-{record['date']}-{serial:03d}")
        path = directory / (record["date"][:7] + ".jsonl")
        lines = documents.setdefault(path, [])
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        changed.add(path)
        keys[record["auto_key"]] = record
        ids[record["id"]] = (path, len(lines)-1, record)
        inserted += 1
    reviewed_count=0
    for identifier, result in (reviews or {}).items():
        path, index, original = ids[identifier]
        if original["status"] != "待验证" or "result" in original:
            continue
        updated = dict(original, status=result["status"], result=result)
        errors = check_record(updated, "复核写入前")
        if errors:
            raise ValueError("；".join(errors))
        assert {k:v for k,v in updated.items() if k not in ("status", "result")} == {k:v for k,v in original.items() if k not in ("status", "result")}
        documents[path][index] = json.dumps(updated, ensure_ascii=False, allow_nan=False) + "\n"
        changed.add(path)
        reviewed_count+=1
    for path in sorted(changed):
        atomic_text(path, "".join(documents[path]))
    return inserted, reviewed_count


def isolated(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root / "logs/test"):
        raise ValueError("隔离输出必须位于仓库logs/test/内")
    return path
