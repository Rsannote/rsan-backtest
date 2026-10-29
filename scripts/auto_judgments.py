#!/usr/bin/env python3
"""已批准规则正式落档；--replay --include-drafts仅在logs/test隔离回放草案。"""
from __future__ import annotations
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from auto_judgment_common import (BJ, TradingCalendar, evaluate, isolated, load_documents, now_bj,
                                  persist, read_csv, read_json, save_report, atomic_text)

ROOT = Path(__file__).resolve().parents[1]
MODULE_CHOICES = ("市场状态", "散户情绪")


def approved(rule):
    value = rule.get("approved")
    return bool(value.strip()) if isinstance(value, str) else value is True and bool(rule.get("approval"))


def validate_rules(config):
    seen = set()
    if config.get("schema_version") != 1 or not isinstance(config.get("rules"), list):
        raise ValueError("自动规则表schema无效")
    for rule in config["rules"]:
        if rule["id"] in seen:
            raise ValueError("规则ID重复")
        seen.add(rule["id"])
        if not isinstance(rule.get("enabled"), bool) or not isinstance(rule.get("approved"), (bool, str)):
            raise ValueError("规则enabled须为布尔值；approved须为布尔值或审核说明")
        if isinstance(rule["approved"], str) and not rule["approved"].strip():
            raise ValueError("审核说明不能为空")
        if rule["enabled"] and not approved(rule):
            raise ValueError("启用正式规则必须有审核说明（或旧格式批准加approval）")
        if rule.get("reserved") and (rule["enabled"] or rule["status"] != "未启用"):
            raise ValueError("主线预留规则必须未启用")
        if rule["trigger"] not in {"market_state", "market_state_enter", "flow_proxy", "sentiment_enter", "mainline_enter"}:
            raise ValueError("未知触发类型")
        n, value = rule["to_trading_days"], rule["value"]
        if isinstance(n,bool) or not isinstance(n,int) or not 1 <= n <= 250:
            raise ValueError("规则交易日长度无效")
        if rule["op"] not in {">", ">=", "<", "<="} or isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
            raise ValueError("规则复核条件无效")


def active(rule, replay, drafts):
    if rule.get("reserved") or rule.get("requires") == "T15":
        return False, "未启用，等待T15；草案回放也不启用"
    if rule["enabled"] and approved(rule):
        return True, None
    if replay and drafts and rule["status"] == "草案待审核":
        return True, None
    return False, "未启用：enabled关闭或尚无审核批准"


def temperature_map(root):
    result = {}
    path = root / "data/社区情绪/温度计.csv"
    if not path.exists():
        return result
    for row in read_csv(path):
        if row["date"] in result:
            raise ValueError("温度日期重复")
        value = float(row["温度"])
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("温度无效")
        result[row["date"]] = value
    return result


def trigger_events(root, rule, day, calendar, temperatures):
    """只读取day及之前的信号；返回事件和无法生成的原因。"""
    kind = rule["trigger"]
    if kind in {"market_state", "market_state_enter", "flow_proxy"}:
        path = Path("data/市场状态") / day[:7] / (day + ".json")
        if not (root / path).exists():
            return [], "市场状态历史存档缺失"
        obj = read_json(root / path)
        if obj.get("数据日期") != day:
            return [], "市场存档数据日期不匹配"
        if kind in {"market_state", "market_state_enter"}:
            if obj.get("市场", {}).get("结论") != rule["state"]:
                return [], "当日未触发该状态"
            if kind == "market_state_enter":
                previous = calendar.previous(day).isoformat()
                before = root / "data/市场状态" / previous[:7] / (previous + ".json")
                if not before.is_file():
                    return [], "前一交易日存档缺失，不能把首次可见当作进入守势"
                old = read_json(before)
                if old.get("数据日期") != previous or old.get("市场", {}).get("结论") not in {"攻", "守", "中性"}:
                    return [], "前一交易日状态不可确认"
                if old["市场"]["结论"] == rule["state"]:
                    return [], "仍处于守势，本日不是进入日"
            return [{"code":"sh000300", "name":"沪深300", "source":str(path),
                     "text":f"{day}市场规则{obj.get('版本')}结论={rule['state']}" + (f"；前一交易日{previous}为{old['市场']['结论']}，本日进入守势" if kind == "market_state_enter" else ""),
                     "type":"指数最大回撤" if kind == "market_state_enter" else "指数涨跌", "direction":rule["state"]}], None
        sector = obj.get("板块", {})
        names, details = sector.get("资金流入前5"), sector.get("明细")
        if not isinstance(names,list) or not isinstance(details,dict) or len(set(names)) != len(names):
            return [], "缺少成交占比变化代理字段/名单无效；未替代为真实资金流"
        if rule.get("source_mode") != "成交占比变化代理":
            return [], "尚无T8真实资金表；规则未批准使用成交占比代理"
        events = []
        for name in names[:rule["top_n"]]:
            detail = details.get(name, {})
            change = detail.get("成交占比变化")
            if isinstance(change,bool) or not isinstance(change,(int,float)) or not math.isfinite(change) or not isinstance(detail.get("code"),str):
                return [], "名单缺有效ETF代码或成交占比变化；不生成部分排名"
            if change <= rule["min_change"]:
                continue
            events.append({"code":detail["code"], "name":name, "source":str(path), "type":"行业ETF相对沪深300",
                           "text":f"{day}成交占比变化代理前{rule['top_n']}：{name} {change:+g}个百分点；{rule['source_metric']}",
                           "direction":"看多"})
        return events, None if events else "前5代理没有超过草案变化阈值的ETF"
    if kind == "sentiment_enter":
        current = temperatures.get(day)
        if current is None:
            return [], "当日温度或前一交易日缺失"
        previous_day = calendar.previous(day).isoformat()
        previous = temperatures.get(previous_day)
        if previous is None:
            return [], "前一交易日温度缺失，无法确认进入"
        threshold = rule["threshold"]
        entered = current < threshold <= previous if rule["zone"] == "冰点" else previous < threshold <= current
        if not entered:
            return [], "当日未进入该温度区间"
        return [{"code":"sh000300", "name":"沪深300", "type":"指数涨跌", "source":"data/社区情绪/温度计.csv",
                 "text":f"{previous_day}温度{previous:g}→{day}温度{current:g}，进入{rule['zone']}；阈值{threshold:g}为待审核方向草案",
                 "direction":"攻" if rule["zone"] == "冰点" else "守"}], None
    return [], "预留主线接口尚未实现"


def make_record(rule, event, day, write_day, calendar):
    target = calendar.window(day, rule["to_trading_days"])[-1].isoformat()
    identity = json.dumps([rule["id"], rule["version"], day, event["code"]], ensure_ascii=False, separators=(",",":"))
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    condition = {"type":event["type"], "code":event["code"], "from":day,
                 "to_trading_days":rule["to_trading_days"], "op":rule["op"], "value":rule["value"]}
    if event["type"] in {"指数相对", "行业ETF相对沪深300"}:
        condition["benchmark"] = "sh000300"
    relative = "起点收盘至窗口最低收盘最大回撤" if event["type"] == "指数最大回撤" else "收盘收益" if event["type"] == "指数涨跌" else "相对沪深300的超额收益"
    claim = f"规则结论：依据{day}指标，{event['name']}未来{rule['to_trading_days']}个交易日{relative} {rule['op']} {rule['value']:.2%}"
    if rule["trigger"] == "flow_proxy":
        claim += "（成交占比变化代理，非真实资金流入）"
    # 临时序号只为内存格式验证；persist按全部档案的当天最大序号分配正式ID。
    return {"id":f"J-{write_day}-001", "auto_key":key, "date":write_day, "auto":True,
            "rule":rule["name"] + "·" + rule["version"], "module":rule["module"],
            "scope":{"type":"产业" if event["type"] == "行业ETF相对沪深300" else "市场", "id":event["code"], "name":event["name"]},
            "horizon":"周" if rule["to_trading_days"]<=10 else "月", "claim":claim,
            "direction":event["direction"], "confidence":"低",
            "evidence":[{"text":event["text"], "source":event["source"], "level":"一级"}],
            "verify":f"固定终点{target}的{relative}{rule['op']}{rule['value']:.2%}",
            "falsify":["完整固定窗口的实际值不满足check条件"], "review_date":target, "check":condition, "status":"待验证"}


def run(root=ROOT, module=None, day=None, replay=False, include_drafts=False, out_dir=None, now=None):
    root = Path(root).resolve()
    now = now or now_bj()
    if now.tzinfo is None:
        raise ValueError("运行时间必须有时区")
    now = now.astimezone(BJ)
    config = read_json(root / "system/自动判断规则.json")
    validate_rules(config)
    calendar = TradingCalendar(root)
    if include_drafts and not replay:
        raise ValueError("include-drafts只允许隔离回放")
    if replay:
        output = isolated(root, out_dir or root / "logs/test/T4")
    else:
        output = root / "logs"
        if out_dir:
            raise ValueError("正式运行不允许自定义档案输出目录")
    archives = sorted({p.stem for p in (root / "data/市场状态").glob("????-??/????-??-??.json")})
    if replay:
        dates = archives
    else:
        source = root / ("data/社区情绪/触发.json" if module == "散户情绪" else "data/市场状态/最新.json")
        if source.exists():
            obj = read_json(source)
            source_day = obj["date" if module == "散户情绪" else "数据日期"]
        else:
            source_day = archives[-1] if archives else calendar.days[-1].isoformat()
        if day and day != source_day:
            raise ValueError("历史指标只允许隔离回放，不能补造正式预测记录")
        dates = [day or source_day]
    if day and replay:
        dates = [d for d in dates if d <= day]
    dates = [d for d in dates if d <= now.date().isoformat()]
    if not dates:
        raise ValueError("无可用市场存档日期")
    temperatures = temperature_map(root)
    report = {"title":"程序判断隔离回放" if replay else "程序判断落档", "generated_at":now.isoformat(),
              "data_date":max(dates), "status":"正常", "module":module, "draft_replay":replay and include_drafts,
              "summary":[], "skipped":[], "errors":[], "rules":[], "pending":[]}
    records = []
    for rule in config["rules"]:
        chosen = module is None or (rule["trigger"] in {"market_state","market_state_enter","flow_proxy"} if module=="市场状态" else rule["trigger"]=="sentiment_enter")
        enabled, reason = active(rule, replay, include_drafts)
        tally = Counter()
        if not chosen or not enabled:
            reason = reason or "本次模块未选择"
            report["skipped"].append(rule["id"] + "：" + reason)
        else:
            for current in dates:
                try:
                    events, missing = trigger_events(root, rule, current, calendar, temperatures)
                    if missing:
                        tally[missing] += 1
                    for event in events:
                        record = make_record(rule,event,current,now.date().isoformat(),calendar)
                        if not replay and (record["review_date"] < now.date().isoformat()
                                           or record["review_date"] <= min(calendar.days[-1],now.date()).isoformat()):
                            raise ValueError("历史信号的复核终点已过或行情已可知，只允许隔离回放，不补造正式预测")
                        records.append(record)
                        tally["生成"] += 1
                except (ValueError, KeyError, OSError) as exc:
                    report["errors"].append(f"{rule['id']} {current}：{exc}")
        report["rules"].append({"id":rule["id"], "name":rule["name"], "enabled":enabled and chosen,
                                "reason":reason, "counts":dict(tally)})
    directory = output / "judgments" if replay else root / "judgments"
    if report["errors"]:
        report["status"] = "失败"
    else:
        inserted, _ = persist(directory, additions=records)
        report["summary"].append(f"本次事件{len(records)}条；新增{inserted}条；同事件重跑跳过{len(records)-inserted}条。")
    if replay:
        documents, ids, _ = load_documents(directory)
        reviews = {}
        for _, _, record in ids.values():
            outcome = evaluate(root, record, min(calendar.days[-1],now.date()), calendar)
            if "pending" in outcome:
                report["pending"].append(record["id"] + "：" + outcome["pending"])
            else:
                outcome["result"]["date"]=now.date().isoformat()
                reviews[record["id"]] = outcome["result"]
        persist(directory, reviews=reviews)
        _, ids, _ = load_documents(directory)
        stats=[]
        for rule in config["rules"]:
            selected=[r for _,_,r in ids.values() if r["rule"]==rule["name"]+"·"+rule["version"]]
            hit=sum(r["status"]=="证实" for r in selected); miss=sum(r["status"]=="证伪" for r in selected)
            enabled,reason=active(rule,replay,include_drafts)
            stats.append({"rule":rule["id"], "events":len(selected), "matured":hit+miss, "hit":hit, "miss":miss,
                          "pending":sum(r["status"]=="待验证" for r in selected), "hit_rate":hit/(hit+miss) if hit+miss else None,
                          "note":reason if not enabled else "低样本，仅作审核依据" if 0<hit+miss<30 else "未有到期完整样本" if not hit+miss else "重叠窗口不等于独立样本"})
        report["statistics"]=stats
        lines=["# T4草案隔离回放命中率", "", "仅用市场状态历史存档日期作为信号日；无模拟交易、无收益策略回测。", "", "|规则|事件|到期完整|证实|证伪|待验证|命中率|说明|", "|---|---:|---:|---:|---:|---:|---|---|"]
        for s in stats:
            lines.append(f"|{s['rule']}|{s['events']}|{s['matured']}|{s['hit']}|{s['miss']}|{s['pending']}|{'暂无' if s['hit_rate'] is None else format(s['hit_rate'],'.2%')}|{s['note']}|")
        lines += ["", "未到期或缺窗口行情不计分母；未启用主线规则始终0事件。正式判断不受本回放影响。", ""]
        atomic_text(output/"命中率.md", "\n".join(lines))
    name="回放_最新" if replay else "程序判断_"+(module or "全部")+"_最新"
    save_report(output,name,report)
    print(f"{report['title']}：{report['data_date']}；事件{len(records)}条；{report['status']}；正式草案未启用不会作为失败。")
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=ROOT)
    parser.add_argument("--module",choices=MODULE_CHOICES)
    parser.add_argument("--date")
    parser.add_argument("--replay",action="store_true")
    parser.add_argument("--include-drafts",action="store_true")
    parser.add_argument("--full-history",action="store_true",help="全历史研究模式，只写logs/test，不落正式档案")
    parser.add_argument("--out-dir",type=Path)
    args=parser.parse_args()
    try:
        if args.full_history:
            if args.date or args.module:
                raise ValueError("全历史模式不接受单日或单模块过滤")
            from auto_judgments_full_history import full_history
            full_history(args.root, args.out_dir)
            return 0
        result=run(args.root,args.module,args.date,args.replay,args.include_drafts,args.out_dir)
    except (ValueError,KeyError,OSError,TypeError) as exc:
        parser.exit(1,f"程序判断失败：{exc}\n")
    return 1 if result["errors"] else 0


if __name__=="__main__":
    raise SystemExit(main())
