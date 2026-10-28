#!/usr/bin/env python3
"""离线计算驱动图谱指标；不抓行情、不修改输入、不作投资判断。

用法：python3 scripts/graph_score.py
审核后显式配置示例：--state-weights '{"高位放缓":0.5,"加速":1.5,"增强":1,"强":1}'
--drawdown-threshold 0.2 只启用回撤这一项规则，不把计划状态解释为买卖结论。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from datetime import date, timedelta
from pathlib import Path


# 仅为审核展示的草案；绝不回填正式 weighted_score / unpriced 字段。
WEIGHT_DRAFTS = {
    "等权草案": {"高位放缓": 1, "加速": 1, "增强": 1, "强": 1},
    "趋势草案": {"高位放缓": 0.5, "加速": 1.5, "增强": 1.25, "强": 1},
}
PRICE_DRAFTS = {"回撤10%草案": 0.10, "回撤20%草案": 0.20, "回撤30%草案": 0.30}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def number(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("价格和复权因子必须为正有限数")
    return result


def weights_value(raw):
    weights = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(weights, dict):
        raise ValueError("状态权重须为 JSON 对象")
    for state, value in weights.items():
        if not isinstance(state, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("状态权重须为 状态:非负有限数字")
        if not math.isfinite(value) or value < 0:
            raise ValueError("状态权重须为非负有限数字")
    return weights


def load_graphs(root, as_of):
    paths = sorted((root / "graph").glob("驱动图谱_*.json"))
    if not paths:
        raise ValueError("缺失 graph/驱动图谱_*.json")
    nodes, families, companies, metadata = {}, {}, {}, []
    threshold, graph_weights = None, None
    for path in paths:
        graph = json.loads(path.read_text(encoding="utf-8"))
        if date.fromisoformat(graph["asOf"]) > as_of:
            raise ValueError(f"{path.name} 晚于计算日期，不能用于历史计算")
        rules = graph["rules"]
        vote_rule = rules.get("vote", "")
        match = re.search(r"强度\s*(?:≥|>=)\s*([123])", vote_rule)
        if not match or "同一家族" not in vote_rule or "1票" not in vote_rule:
            raise ValueError(f"不支持的投票规则：{path.name}；请先审核规则格式")
        current = int(match.group(1))
        if threshold is not None and threshold != current:
            raise ValueError("多份图谱的投票阈值冲突")
        threshold = current
        current_weights = weights_value(rules.get("state_weights", {}))
        if graph_weights is not None and current_weights != graph_weights:
            raise ValueError("多份图谱的状态权重冲突")
        graph_weights = current_weights
        for family, name in graph["families"].items():
            if family in families and families[family] != name:
                raise ValueError(f"家族定义冲突：{family}")
            families[family] = name
        for node in graph["drivers"] + graph.get("transmission", []):
            key = node["id"]
            if key in nodes and nodes[key] != node:
                raise ValueError(f"驱动/传导节点定义冲突：{key}")
            nodes[key] = node
        for company in graph["companies"]:
            code = company["code"]
            if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
                raise ValueError("公司代码必须是六位字符串，不能丢失前导零")
            if code in companies and companies[code] != company:
                raise ValueError(f"公司定义冲突：{code}；不自动合并不同版本")
            companies[code] = company
        metadata.append({"path": str(path.relative_to(root)), "as_of": graph["asOf"],
                         "version": graph["version"], "rules": rules})
    for node in nodes.values():
        if "family" in node and node["family"] not in families:
            raise ValueError(f"未知家族：{node['family']}")
    return paths, nodes, companies, metadata, threshold, graph_weights


def expand_driver(node_id, nodes, trail=()):
    """传导节点递归追溯源驱动；保留公司边的强度，不增造传导边强度。"""
    if node_id in trail:
        raise ValueError("传导节点成环：" + "→".join(trail + (node_id,)))
    if node_id not in nodes:
        raise ValueError(f"公司边引用未知节点：{node_id}")
    node = nodes[node_id]
    if "family" in node:
        return [(node, list(trail + (node_id,)))]
    if not node.get("from"):
        raise ValueError(f"传导节点缺失来源：{node_id}")
    return [item for source in node["from"] for item in expand_driver(source, nodes, trail + (node_id,))]


def contributions(company, nodes, threshold):
    result = {}
    for edge in company["links"]:
        if len(edge) != 3:
            raise ValueError(f"公司边格式错误：{company['code']}")
        node_id, strength, lag = edge
        if isinstance(strength, bool) or strength not in (1, 2, 3):
            raise ValueError("边强度仅支持 1、2、3")
        for driver, path in expand_driver(node_id, nodes):
            family = driver["family"]
            result.setdefault(family, []).append({"driver": driver["id"], "state": driver["state"],
                "strength": strength, "lag": lag, "path": path, "vote_eligible": strength >= threshold,
                "note": driver.get("note")})
    return [{"family": family, "vote": int(any(c["vote_eligible"] for c in items)),
             "max_strength": max(c["strength"] for c in items), "components": items}
            for family, items in sorted(result.items())]


def weighted_score(families, weights):
    total, missing, details = 0.0, set(), []
    for family in families:
        eligible = [c for c in family["components"] if c["vote_eligible"]]
        if not eligible:
            continue
        unknown = {c["state"] for c in eligible if c["state"] not in weights}
        missing.update(unknown)
        score = None if unknown else max(c["strength"] * weights[c["state"]] for c in eligible)
        details.append({"family": family["family"], "score": score, "missing_states": sorted(unknown)})
        if score is not None:
            total += score
    return {"score": None if missing else round(total, 6), "known_family_subtotal": round(total, 6),
            "missing_states": sorted(missing), "families": details}


def pricing(root, code, as_of, calendar):
    path = root / "data/行情库/股票" / (code + ".csv")
    result = {"source": str(path.relative_to(root)), "status": "缺失", "reason": None,
              "price_date": None, "drawdown_52w": None, "latest_close": None,
              "high_52w_adjusted": None, "window_start": (as_of - timedelta(weeks=52) + timedelta(days=1)).isoformat(),
              "window_end": as_of.isoformat(), "observations": 0}
    if not path.exists():
        result["reason"] = "行情文件缺失"
        return result
    try:
        rows = read_csv(path)
        dated = {}
        for row in rows:
            day = date.fromisoformat(row["date"])
            if day in dated:
                raise ValueError("存在重复行情日期")
            if day <= as_of:
                dated[day] = row
        start = date.fromisoformat(result["window_start"])
        window = sorted(day for day in dated if start <= day <= as_of)
        if not window:
            raise ValueError("窗口内无行情")
        highs = [number(dated[day]["high"]) * number(dated[day]["factor"]) for day in window]
        latest = window[-1]
        close = number(dated[latest]["close"])
        adjusted_close = close * number(dated[latest]["factor"])
        high = max(highs)
        if adjusted_close > high * (1 + 1e-9):
            raise ValueError("收盘复权价大于窗口最高复权价")
        result.update(price_date=latest.isoformat(), latest_close=close, high_52w_adjusted=high,
                      observations=len(window))
        issues = []
        if latest != as_of:
            issues.append("行情未到计算日（可能缺报或停牌，不能区分）")
        if not any(day < start for day in dated):
            issues.append("历史不足52周，无法确认完整窗口")
        missing_days = sorted(day for day in calendar if start <= day <= as_of and day not in dated)
        if missing_days:
            issues.append(f"相对指数日历缺失{len(missing_days)}日（可能停牌，未补零）")
        result["drawdown_52w"] = round(1 - adjusted_close / high, 8)
        result["status"] = "完整" if not issues else "不完整"
        result["reason"] = "；".join(issues) or None
        result["missing_dates"] = [day.isoformat() for day in missing_days]
    except (ValueError, KeyError, TypeError) as error:
        result.update(status="无效", reason=str(error), drawdown_52w=None)
    return result


def render(report):
    lines = ["# 驱动图谱打分指标", "", f"计算日期：{report['as_of']}（北京时间）；仅使用仓库已存数据。",
             "", "正式状态权重与未被定价阈值未配置时，相关值为缺失/待审核。草案场景不作正式结论。",
             "排序：独立家族票数降序 → 正式状态加权分降序（已配置且完整的在前） → 家族最大强度和降序 → 代码。",
             "", "|代码|公司|票数|计票家族|正式加权分|等权草案|趋势草案|距52周高点回撤|行情日期/质量|计划状态/日期|交叉且未定价|",
             "|---|---|---:|---|---:|---:|---:|---:|---|---|---|"]
    def score(value):
        return "缺失/待审核" if value is None else f"{value:g}"
    for row in report["companies"]:
        price, pool = row["pricing"], row["pool"]
        dd = price["drawdown_52w"]
        families = "、".join(f["family"] for f in row["families"] if f["vote"])
        candidate = row["cross_candidate"]
        lines.append(f"|{row['code']}|{row['name']}|{row['votes']}|{families}|{score(row['weighted_score'])}|"
                     f"{score(row['weight_drafts']['等权草案']['score'])}|{score(row['weight_drafts']['趋势草案']['score'])}|"
                     f"{'缺失' if dd is None else format(dd, '.2%')}|{price['price_date'] or '缺失'}/{price['status']}|"
                     f"{pool['plan_status'] or '缺失'}/{pool['plan_price_date'] or '缺失'}|"
                     f"{'【待判读】' if candidate is None else ('符合显式规则' if candidate else '不符合显式规则')}|")
    lines += ["", "## 口径与缺失", "",
              "- 强度达到图谱 rules.vote 阈值才计票，同一家族只计一票；待补状态不撤销票数。",
              "- 传导节点追溯 from 到驱动家族，继承公司→传导节点边强度；不额外增票。AI 与 OVERSEA 的相关性保留源注释，不擅自合并家族。",
              "- 得分为每个计票家族 max(合格边强度×该驱动状态权重) 之和；任一合格状态无权重则正式总分为 null，已知家族小计单列。",
              "- 回撤=1−最新收盘×当日factor/max(窗口内high×当日factor)；窗口为计算日向前52周、左开右闭，共364自然日。",
              "- 行情不完整仍展示已有窗口回撤，但不据此标记候选；空计划状态、缺失行情与未配置阈值使用 null，绝不填0。",
              "- 计划状态照录股票池，plan_price_date 仅为计划价格日期，不冒充计划状态更新时间；股票池快照日期独立记录。",
              "- 10%/20%/30% 仅为待审核回撤场景，JSON 分别展示条件结果；计划状态仅供判读，不解释为定价结论。",
              "- 规则、输入文件文本规范化 SHA256（UTF-8，换行转LF，文件末尾统一一个换行）、每条边与家族状态分解均见同名 JSON；T5 读取 companies / cross_candidate / pricing / pool。",
              "", "## 状态权重草案（未启用）", "",
              "|草案|高位放缓|加速|增强|强|待补|", "|---|---:|---:|---:|---:|---|",
              "|等权草案|1|1|1|1|缺失|", "|趋势草案|0.5|1.5|1.25|1|缺失|", "",
              "## 定价场景（未启用）", "",
              "以下只筛选票数≥2、行情完整且有计划状态的公司，不解释计划状态；仅展示条件比较。", "",
              "|草案|满足场景条件的公司|", "|---|---|"]
    for name in PRICE_DRAFTS:
        matches = [r["code"] + " " + r["name"] for r in report["companies"] if r["pricing_drafts"][name]["cross_candidate"] is True]
        lines.append(f"|{name}|{'、'.join(matches) or '无'}|")
    lines += ["", "## 数据提示", ""]
    lines.extend("- " + item for item in report["warnings"])
    lines += ["", "程序只输出指标，产业判断与正式定价判读：【待判读】。不构成投资建议。", ""]
    return "\n".join(lines)


def build(root, as_of=None, explicit_weights=None, drawdown_threshold=None):
    calendar_path = root / "data/行情库/指数/sh000300.csv"
    if not calendar_path.exists():
        raise ValueError("缺失沪深300交易日历；请提供完整离线输入")
    calendar = {date.fromisoformat(row["date"]) for row in read_csv(calendar_path)}
    if not calendar:
        raise ValueError("交易日历为空")
    as_of = date.fromisoformat(as_of) if as_of else max(calendar)
    if as_of not in calendar:
        raise ValueError("计算日不在已存沪深300交易日历中")
    paths, nodes, companies, graphs, threshold, graph_weights = load_graphs(root, as_of)
    weights = weights_value(explicit_weights) if explicit_weights is not None else graph_weights
    pool_path, pool_meta = root / "data/股票池/股票.csv", root / "data/股票池/状态.json"
    pool_rows = read_csv(pool_path) if pool_path.exists() else []
    pool = {}
    for row in pool_rows:
        if row["code"] in pool:
            raise ValueError(f"股票池代码重复：{row['code']}")
        pool[row["code"]] = row
    pool_date = json.loads(pool_meta.read_text(encoding="utf-8")).get("数据日期") if pool_meta.exists() else None
    warnings = ["正式状态权重：" + ("显式配置" if weights else "缺失，待审核"),
                "未被定价阈值：" + (str(drawdown_threshold) if drawdown_threshold is not None else "未配置，待审核"),
                f"图谱日期：{'、'.join(g['as_of'] for g in graphs)}；股票池快照日期：{pool_date or '缺失'}",
                "传导强度继承、家族最大值聚合与状态权重由审核确认；JSON 保留逐边证据。"]
    rows = []
    used_paths = paths + [calendar_path, pool_path, pool_meta]
    for code, company in companies.items():
        family_rows = contributions(company, nodes, threshold)
        votes = sum(f["vote"] for f in family_rows)
        weighted = weighted_score(family_rows, weights)
        price = pricing(root, code, as_of, calendar)
        used_paths.append(root / price["source"])
        source_pool = pool.get(code, {})
        pool_valid = bool(pool_date and date.fromisoformat(pool_date) <= as_of)
        pool_info = {"source": "data/股票池/股票.csv", "snapshot_date": pool_date,
                     "status": "已匹配" if source_pool and pool_valid else "缺失",
                     "reason": None if source_pool and pool_valid else "未匹配公司或快照日期缺失/晚于计算日",
                     "plan_status": (source_pool.get("plan_status") or None) if pool_valid else None,
                     "plan_price_date": (source_pool.get("plan_priceDate") or None) if pool_valid else None,
                     "l1_status": (source_pool.get("l1_status") or None) if pool_valid else None}
        usable = price["status"] == "完整" and pool_info["plan_status"] is not None
        unpriced = (price["drawdown_52w"] >= drawdown_threshold) if usable and drawdown_threshold is not None else None
        drafts = {name: {"threshold": value, "unpriced_condition": price["drawdown_52w"] >= value if usable else None,
                         "cross_candidate": (votes >= 2 and price["drawdown_52w"] >= value) if usable else None}
                  for name, value in PRICE_DRAFTS.items()}
        rows.append({"code": code, "name": company["name"], "node": company["node"], "votes": votes,
                     "families": family_rows, "strength_score": sum(f["max_strength"] for f in family_rows if f["vote"]),
                     "weighted_score": weighted["score"], "weighted_detail": weighted,
                     "weight_drafts": {name: weighted_score(family_rows, draft) for name, draft in WEIGHT_DRAFTS.items()},
                     "pricing": price, "pool": pool_info, "unpriced": unpriced,
                     "cross_candidate": (votes >= 2 and unpriced) if unpriced is not None else None,
                     "pricing_drafts": drafts})
        if price["status"] != "完整":
            warnings.append(f"{code} {company['name']}：{price['reason']}")
        if pool_info["plan_status"] is None:
            warnings.append(f"{code} {company['name']}：计划状态缺失")
    rows.sort(key=lambda row: (-row["votes"], row["weighted_score"] is None,
                              -(row["weighted_score"] or 0), -row["strength_score"], row["code"]))
    sources = [{"path": str(path.relative_to(root)), "sha256_text": hashlib.sha256((path.read_text(encoding="utf-8").rstrip("\n") + "\n").encode("utf-8")).hexdigest() if path.exists() else None}
               for path in sorted(set(used_paths))]
    return {"schema_version": "1.0", "as_of": as_of.isoformat(), "timezone": "Asia/Shanghai",
            "kind": "指标，非投资结论", "graphs": graphs, "sources": sources,
            "rules": {"vote_strength_threshold": threshold, "state_weights": weights or None,
                      "state_weights_source": "命令行显式配置" if explicit_weights is not None else "图谱rules或缺失",
                      "score_aggregation": "合格边强度×状态权重，家族内取最大值，家族间求和",
                      "drawdown_threshold": drawdown_threshold, "pricing_status": "显式回撤规则" if drawdown_threshold is not None else "待审核",
                      "weight_drafts_enabled": False, "pricing_drafts_enabled": False},
            "warnings": warnings, "companies": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="离线仓库根目录")
    parser.add_argument("--as-of", help="计算日 YYYY-MM-DD，默认已存沪深300最新交易日")
    parser.add_argument("--state-weights", help="审核后显式配置的 JSON 状态权重")
    parser.add_argument("--drawdown-threshold", type=float, help="审核后显式配置回撤比例，例如0.2")
    parser.add_argument("--out-dir", type=Path, help="默认根目录 graph；验证时可输出到临时目录")
    args = parser.parse_args()
    try:
        if args.drawdown_threshold is not None and not (0 <= args.drawdown_threshold <= 1):
            raise ValueError("回撤阈值必须在0至1之间")
        report = build(args.root.resolve(), args.as_of, args.state_weights, args.drawdown_threshold)
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.exit(1, f"图谱打分失败：{error}\n")
    out = args.out_dir or args.root / "graph"
    out.mkdir(parents=True, exist_ok=True)
    (out / "打分_最新.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out / "打分_最新.md").write_text(render(report), encoding="utf-8")
    print(f"计算日期 {report['as_of']}；公司 {len(report['companies'])} 家；票数≥2 {sum(r['votes'] >= 2 for r in report['companies'])} 家")
    print(f"输出：{out / '打分_最新.md'}；{out / '打分_最新.json'}")
    print("正式权重/阈值未配置的部分保留待审核；草案未启用。")


if __name__ == "__main__":
    main()
