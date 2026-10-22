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
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = {"行情资金", "新闻政策", "财报产业", "宏观数据", "市场状态", "驱动图谱", "产业推演", "政策分析",
           "资金轮动", "大盘情景", "个股研判", "仓位风控", "判断档案", "回测引擎", "总控"}
SCOPE_TYPES = {"市场", "产业", "驱动", "公司"}
HORIZONS = {"日", "周", "月", "季", "年"}
DIRECTIONS = {"攻", "守", "中性", "看多", "看空"}
CONFIDENCE = {"高", "中", "低"}
LEVELS = {"一级", "二级", "三级"}
STATUSES = {"待验证", "证实", "证伪", "部分", "无法判定", "已被取代"}
REQUIRED = ["id", "date", "module", "scope", "horizon", "claim", "direction", "confidence",
            "evidence", "verify", "falsify", "review_date", "status"]
ID_RE = re.compile(r"^J-\d{4}-\d{2}-\d{2}-\d{3}$")


def parse_date(s):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def check_record(r, where):
    errs = []
    for k in REQUIRED:
        if k not in r or r[k] in ("", [], None):
            errs.append(f"缺少 {k}")
    if errs:
        return [f"{where} {r.get('id', '?')}：{e}" for e in errs]
    if not ID_RE.match(r["id"]):
        errs.append(f"id 格式不对：{r['id']}")
    elif r["id"][2:12] != r["date"]:
        errs.append("id 里的日期和 date 不一致")
    d, rd = parse_date(r["date"]), parse_date(r["review_date"])
    if not d:
        errs.append(f"date 不是 YYYY-MM-DD：{r['date']}")
    if not rd:
        errs.append(f"review_date 不是 YYYY-MM-DD：{r['review_date']}")
    if d and rd and rd < d:
        errs.append("复核日早于写入日")
    if r["module"] not in MODULES:
        errs.append(f"未知模块：{r['module']}")
    sc = r["scope"]
    if not isinstance(sc, dict) or sc.get("type") not in SCOPE_TYPES or not sc.get("name"):
        errs.append("scope 需要 type（市场/产业/驱动/公司）和 name")
    for k, allowed in (("horizon", HORIZONS), ("direction", DIRECTIONS),
                       ("confidence", CONFIDENCE), ("status", STATUSES)):
        if r[k] not in allowed:
            errs.append(f"{k} 取值不对：{r[k]}")
    ev = r["evidence"]
    if not isinstance(ev, list):
        errs.append("evidence 必须是列表")
    else:
        lv = []
        for e in ev:
            if not isinstance(e, dict) or not e.get("text") or not e.get("source") or e.get("level") not in LEVELS:
                errs.append("evidence 每条需要 text、source、level（一级/二级/三级）")
                break
            lv.append(e["level"])
        if lv and all(x == "三级" for x in lv):
            errs.append("证据只有三级来源，不能单独支撑判断")
    if not isinstance(r["falsify"], list):
        errs.append("falsify 必须是列表")
    if r["status"] not in ("待验证", "已被取代"):
        res = r.get("result")
        if not isinstance(res, dict) or not res.get("date") or not res.get("note"):
            errs.append("已复核的判断需要 result（date、status、note）")
    return [f"{where} {r.get('id', '?')}：{e}" for e in errs]


def load():
    recs = []
    for path in sorted(glob.glob(os.path.join(ROOT, "judgments", "*.jsonl"))):
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
    a = ap.parse_args()
    today = parse_date(a.today) if a.today else dt.date.today()

    recs = load()
    errs, ids = [], {}
    for where, r in recs:
        if "_bad" in r:
            errs.append(f"{where}：不是合法 JSON（{r['_bad']}）")
            continue
        errs += check_record(r, where)
        if r.get("id") in ids:
            errs.append(f"{where}：id 重复，和 {ids[r['id']]} 冲突")
        ids[r.get("id")] = where
    known = set(ids)
    for where, r in recs:
        for k in ("depends_on", "supersedes"):
            v = r.get(k)
            for ref in (v if isinstance(v, list) else [v] if v else []):
                if ref not in known:
                    errs.append(f"{where} {r.get('id')}：{k} 指向不存在的 {ref}")

    good = [r for _, r in recs if "_bad" not in r]
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
                for key in (f"模块·{r['module']}", f"置信·{r['confidence']}"):
                    tally[key][s] += 1
        print("\n命中率（证实＝1，部分＝0.5）：")
        if not tally:
            print(" 还没有复核完成的判断")
        for key in sorted(tally):
            t = tally[key]
            n = t["证实"] + t["证伪"] + t["部分"]
            print(f" - {key}：{(t['证实'] + 0.5 * t['部分']) / n:.0%}（{n} 条）")

    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
