"""检查判断档案 judgments/*.jsonl 的格式，并列出到期待复核的判断。

用法：
  python3 scripts/check_judgments.py           # 检查全部记录
  python3 scripts/check_judgments.py --due     # 另外列出复核日已到、仍待验证的判断
  python3 scripts/check_judgments.py --stats   # 按模块和置信度统计命中率

格式说明见 system/判断记录格式.md。只用标准库。
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import math
from pathlib import PurePosixPath
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = {"行情资金", "新闻政策", "财报产业", "宏观数据", "市场状态", "驱动图谱", "产业推演", "政策分析",
           "资金轮动", "大盘情景", "个股研判", "仓位风控", "判断档案", "回测引擎", "总控", "散户情绪", "主线雷达", "社区情绪", "信息源监控"}
SCOPE_TYPES = {"市场", "产业", "驱动", "公司"}
HORIZONS = {"日", "周", "月", "季", "年"}
DIRECTIONS = {"攻", "守", "中性", "看多", "看空"}
CONFIDENCE = {"高", "中", "低"}
LEVELS = {"一级", "二级", "三级"}
STATUSES = {"待验证", "证实", "证伪", "部分", "无法判定", "已被取代"}
REQUIRED = ["id", "date", "module", "scope", "horizon", "claim", "direction", "confidence",
            "evidence", "verify", "falsify", "review_date", "status"]
ID_RE = re.compile(r"^J-\d{4}-\d{2}-\d{2}-\d{3}$")


def check_condition(c):
    if not isinstance(c, dict):
        return ["check必须是对象"]
    errors = []
    kind = c.get("type")
    if not isinstance(kind, str) or kind not in {"指数涨跌", "指数相对", "行业ETF相对沪深300", "指数最大回撤"}:
        errors.append("check.type不受支持")
    code = c.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"(?:sh|sz)\d{6}" if kind != "行业ETF相对沪深300" else r"\d{6}", code):
        errors.append("check.code格式不对")
    if isinstance(kind, str) and kind in {"指数相对", "行业ETF相对沪深300"}:
        benchmark = c.get("benchmark")
        if not isinstance(benchmark, str) or not re.fullmatch(r"(?:sh|sz)\d{6}", benchmark):
            errors.append("check.benchmark需要指数代码")
        if kind == "行业ETF相对沪深300" and benchmark != "sh000300":
            errors.append("行业ETF基准必须是sh000300")
    if not parse_date(c.get("from")):
        errors.append("check.from不是日期")
    n = c.get("to_trading_days")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 250:
        errors.append("check.to_trading_days须为1–250的整数")
    if not isinstance(c.get("op"), str) or c.get("op") not in {">", ">=", "<", "<="}:
        errors.append("check.op不受支持")
    v = c.get("value")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        errors.append("check.value须为有限数字")
    elif kind == "指数最大回撤" and not 0 <= v <= 1:
        errors.append("指数最大回撤阈值须在0–1之间")
    return errors


def parse_date(s):
    try:
        if not isinstance(s,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",s):
            return None
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def check_record(r, where):
    errs = []
    if not isinstance(r, dict):
        return [f"{where}：记录须为对象"]
    for k in REQUIRED:
        if k not in r or r[k] in ("", [], None):
            errs.append(f"缺少 {k}")
    if errs:
        return [f"{where} {r.get('id', '?')}：{e}" for e in errs]
    if not isinstance(r["id"], str) or not ID_RE.match(r["id"]):
        errs.append(f"id 格式不对：{r['id']}")
    elif r["id"][2:12] != r["date"]:
        errs.append("id 里的日期和 date 不一致")
    d, rd = parse_date(r["date"]), parse_date(r["review_date"])
    if not d:
        errs.append(f"date 不是 YYYY-MM-DD：{r['date']}")
    if not rd:
        errs.append(f"review_date 不是 YYYY-MM-DD：{r['review_date']}")
    if d and rd and rd < d and not r.get("auto"):
        errs.append("复核日早于写入日")
    if not isinstance(r["module"], str) or r["module"] not in MODULES:
        errs.append(f"未知模块：{r['module']}")
    sc = r["scope"]
    if not isinstance(sc, dict) or not isinstance(sc.get("type"), str) or sc.get("type") not in SCOPE_TYPES or not sc.get("name"):
        errs.append("scope 需要 type（市场/产业/驱动/公司）和 name")
    for k, allowed in (("horizon", HORIZONS), ("direction", DIRECTIONS),
                       ("confidence", CONFIDENCE), ("status", STATUSES)):
        if not isinstance(r[k], str) or r[k] not in allowed:
            errs.append(f"{k} 取值不对：{r[k]}")
    ev = r["evidence"]
    if not isinstance(ev, list):
        errs.append("evidence 必须是列表")
    else:
        lv = []
        for e in ev:
            if not isinstance(e, dict) or not e.get("text") or not e.get("source") or not isinstance(e.get("level"),str) or e.get("level") not in LEVELS:
                errs.append("evidence 每条需要 text、source、level（一级/二级/三级）")
                break
            lv.append(e["level"])
        if lv and all(x == "三级" for x in lv):
            errs.append("证据只有三级来源，不能单独支撑判断")
    if not isinstance(r["falsify"], list):
        errs.append("falsify 必须是列表")
    if "auto" in r and not isinstance(r["auto"], bool):
        errs.append("auto须为布尔值")
    if "rule" in r and (not isinstance(r["rule"], str) or not r["rule"].strip()):
        errs.append("rule须为非空规则名和版本字符串")
    if "check" in r:
        errs += check_condition(r["check"])
        start = parse_date(r["check"].get("from")) if isinstance(r["check"], dict) else None
        if start and d and start > d:
            errs.append("check.from晚于写入日期")
        if start and rd and rd <= start:
            errs.append("复核日须晚于check.from")
    if r.get("auto") is True:
        if not r.get("rule") or "check" not in r:
            errs.append("程序记录须有rule与check")
        if r["confidence"] != "低":
            errs.append("程序记录confidence恒为低")
        if not isinstance(r.get("auto_key"), str) or not re.fullmatch(r"[0-9a-f]{16}", r.get("auto_key", "")):
            errs.append("程序记录须有16位auto_key")
        for e in ev if isinstance(ev, list) else []:
            source = e.get("source", "") if isinstance(e, dict) else ""
            if not isinstance(source, str) or not source.startswith("data/") or ".." in PurePosixPath(source).parts or e.get("level") != "一级":
                errs.append("程序证据须引用data/相对路径且level=一级")
    if r["status"] not in ("待验证", "已被取代"):
        res = r.get("result")
        if not isinstance(res, dict) or not parse_date(res.get("date")) or not res.get("note") or res.get("status") != r["status"] or not isinstance(res.get("evidence"), list) or not res["evidence"]:
            errs.append("已复核的判断需要 result（date、status、note）")
    return [f"{where} {r.get('id', '?')}：{e}" for e in errs]


def load(directory=None):
    recs = []
    for path in sorted(glob.glob(os.path.join(directory or os.path.join(ROOT, "judgments"), "*.jsonl"))):
        name = os.path.relpath(path, ROOT)
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append((f"{name}:{i}", json.loads(line)))
                except json.JSONDecodeError as e:
                    recs.append((f"{name}:{i}", {"_bad": str(e)}))
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--due", action="store_true", help="列出到期待复核的判断")
    ap.add_argument("--stats", action="store_true", help="统计命中率")
    ap.add_argument("--today", help="以某天为今天（测试用）")
    ap.add_argument("--directory", help="检查隔离档案目录")
    a = ap.parse_args()
    today = parse_date(a.today) if a.today else dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    if today is None:
        ap.error("--today须为YYYY-MM-DD")

    recs = load(a.directory)
    errs, ids = [], {}
    for where, r in recs:
        if not isinstance(r, dict) or "_bad" in r:
            errs.append(f"{where}：不是合法JSON对象")
            continue
        errs += check_record(r, where)
        if not isinstance(r.get("id"), str):
            continue
        if r.get("id") in ids:
            errs.append(f"{where}：id 重复，和 {ids[r['id']]} 冲突")
        ids[r.get("id")] = where
    known = set(ids)
    for where, r in recs:
        if not isinstance(r, dict):
            continue
        for k in ("depends_on", "supersedes"):
            v = r.get(k)
            for ref in (v if isinstance(v, list) else [v] if v else []):
                if ref not in known:
                    errs.append(f"{where} {r.get('id')}：{k} 指向不存在的 {ref}")

    good = [r for _, r in recs if isinstance(r, dict) and not check_record(r, "检查")]
    print(f"共 {len(good)} 条判断，待验证 {sum(r.get('status') == '待验证' for r in good)} 条")
    if errs:
        print(f"\n发现 {len(errs)} 个问题：")
        for e in errs:
            print(" -", e)

    if a.due:
        due = sorted((r for r in good if r.get("status") == "待验证"
                      and parse_date(r.get("review_date")) and parse_date(r["review_date"]) <= today),
                     key=lambda r: r["review_date"])
        print(f"\n到期待复核（截至 {today}）：{len(due)} 条")
        for r in due:
            print(f" - {r['review_date']} {r['id']} [{r['module']}] {r['scope']['name']}：{r['claim']}")

    if a.stats:
        tally = defaultdict(lambda: defaultdict(int))
        for r in good:
            s = r.get("status")
            if s in ("证实", "证伪", "部分"):
                for key in (f"{r['module']} × {'程序' if r.get('auto') else '手写'} × {r['confidence']}",):
                    tally[key][s] += 1
        print("\n命中率（模块×是否程序×置信度；证实＝1，部分＝0.5；无法判定不计分母）：")
        if not tally:
            print(" 还没有复核完成的判断")
        for key in sorted(tally):
            t = tally[key]
            n = t["证实"] + t["证伪"] + t["部分"]
            print(f" - {key}：{(t['证实'] + 0.5 * t['部分']) / n:.0%}（{n} 条）")

    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
