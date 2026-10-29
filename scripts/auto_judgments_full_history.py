"""全历史研究回放；复现审核统计并区分缓存回撤、起点回撤和进入事件。"""
import datetime as dt
import hashlib
import math
from pathlib import Path

from auto_judgment_common import TradingCalendar, atomic_text, isolated, now_bj, prices, read_csv
import json


def number(value):
    if value in (None, "", "nan", "NaN"):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("历史指标含无穷或无效数值")
    return result


def statistic(name, values, predicate):
    complete = [v for v in values if v is not None]
    hit = sum(predicate(v) for v in complete)
    return {"name": name, "samples": len(values), "complete": len(complete), "pending": len(values)-len(complete),
            "hit": hit, "review_share": hit/len(values) if values else None,
            "hit_rate": hit/len(complete) if complete else None}


def index_metrics(rows, calendar, start, n, as_of):
    """严格固定窗口；不会用第N条已存行情顺延缺日。"""
    dates = calendar.window(start, n)
    if dates[-1] > as_of:
        return None, None
    values = []
    for day in dates:
        row = rows.get(day.isoformat())
        if row is None:
            return None, None
        close, factor = number(row["close"]), number(row["factor"])
        if close is None or factor is None or close <= 0 or factor <= 0:
            return None, None
        values.append(close*factor)
    return values[-1]/values[0]-1, 1-min(values)/values[0]


def previous_day(calendar, day):
    try:
        return calendar.previous(day).isoformat()
    except ValueError:
        return None  # 输入首日之前的日历未知，不把首次可见当作进入。


def table(lines, rows):
    percent = lambda v: "暂无" if v is None else f"{100*v:.1f}%"
    lines += ["| 指标 | 全样本 | 完整 | 待到期/缺日 | 命中 | 复现评论的总样本占比 | 完整样本命中率 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['name']} | {r['samples']} | {r['complete']} | {r['pending']} | {r['hit']} | {percent(r['review_share'])} | {percent(r['hit_rate'])} |")


def full_history(root, out_dir=None):
    root = Path(root).resolve()
    output = isolated(root, out_dir or root/"logs/test/T4")
    state_path = root/"logs/市场状态_规则检验_逐日.csv"
    temp_path = root/"data/社区情绪/温度计.csv"
    market = read_csv(state_path)
    temperatures = read_csv(temp_path)
    if not market or not temperatures:
        raise ValueError("全历史状态或温度表为空，不生成空白成功报告")
    states, temps = {}, {}
    for r in market:
        dt.date.fromisoformat(r["date"])
        if r["date"] in states or r["结论"] not in {"攻", "守", "中性"}:
            raise ValueError("全历史状态日期重复或状态非法")
        states[r["date"]] = r
    for r in temperatures:
        dt.date.fromisoformat(r["date"])
        value = number(r["温度"])
        if r["date"] in temps or value is None or not 0 <= value <= 100:
            raise ValueError("温度日期重复或温度非法")
        temps[r["date"]] = value
    calendar = TradingCalendar(root)
    as_of = min(calendar.days[-1], now_bj().date())
    price, price_path = prices(root, "指数涨跌", "sh000300")
    if max(states) > as_of.isoformat() or max(temps) > now_bj().date().isoformat():
        raise ValueError("输入指标日期在可用边界之后")

    # 评论所引用的指标字段是百分数；缺未来窗口保留None，绝不填0。
    cached = []
    for group in ("全部", "攻", "守", "中性"):
        subset = market if group == "全部" else [r for r in market if r["结论"] == group]
        cached.append(statistic(group+"之后5日上涨", [number(r["5日涨跌"]) for r in subset], lambda v:v>0))
        cached.append(statistic(group+"20日滚动高点回撤≥5%", [number(r["20日最大回撤"]) for r in subset], lambda v:v<=-5))

    starting = []
    for group in ("攻", "守", "中性"):
        values = [index_metrics(price, calendar, r["date"], 20, as_of)[1] for r in market if r["结论"] == group]
        starting.append(statistic(group+"每天：新check起点回撤≥5%", values, lambda v:v>=.05))

    entries, unknown = [], []
    for day, row in sorted(states.items()):
        if row["结论"] != "守":
            continue
        previous = previous_day(calendar, day)
        if previous not in states:
            unknown.append(day)
            continue
        if states[previous]["结论"] == "守":
            continue
        value = index_metrics(price, calendar, day, 20, as_of)[1]
        entries.append({"date": day, "previous": previous, "previous_state": states[previous]["结论"], "drawdown": value})
    risk = statistic("正式新规则：仅进入守势日，20日起点回撤≥5%", [r["drawdown"] for r in entries], lambda v:v>=.05)

    sentiment, events = [], {}
    for zone in ("冰点", "过热"):
        selected = []
        for day, value in sorted(temps.items()):
            previous = temps.get(previous_day(calendar, day))
            if previous is None:
                continue
            entered = value < 20 <= previous if zone == "冰点" else previous < 80 <= value
            if entered:
                ret = index_metrics(price, calendar, day, 20, as_of)[0]
                selected.append({"date": day, "temperature": value, "previous_temperature": previous, "return": ret})
        events[zone] = selected
        sentiment.append(statistic("进入冰点(<20)后20日上涨" if zone == "冰点" else "进入过热(≥80)后20日不涨",
                                   [r["return"] for r in selected], (lambda v:v>0) if zone == "冰点" else (lambda v:v<=0)))

    files = (state_path, temp_path, root/price_path)
    report = {"generated_at": now_bj().isoformat(), "price_as_of": as_of.isoformat(),
              "market_dates": [min(states), max(states)], "temperature_dates": [min(temps), max(temps)],
              "sources": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
              "cached_daily": cached, "starting_daily": starting, "entry_risk": risk, "entry_events": entries,
              "entry_previous_unknown": unknown, "sentiment": sentiment, "sentiment_events": events,
              "scope": "研究统计，不写正式判断；仅进入守势风险与成交占比代理前五获批，其他统计不代表启用"}
    lines = ["# T4 全历史回放", "", report["scope"], "",
             f"市场状态：{min(states)}—{max(states)}，{len(states)}行；温度：{min(temps)}—{max(temps)}，{len(temps)}行；已存行情截至{as_of}。", "",
             "## 复现PR #6评论引用的历史表", "",
             "以下直接读取规则检验CSV的5日涨跌/20日最大回撤。原表回撤为滚动高点回撤，不是新check的起点回撤。评论使用所有状态日作为分母；复现占比将缺未来窗口留在总分母中，但不把它当0或命中。右列另给仅完整样本的命中率，待到期/缺日不能计入正式评分。", ""]
    table(lines, cached)
    lines += ["", "## 新check与正式进入事件", "",
              "按close×factor起点到最低收盘计算，包含起点，缺日不顺延。行情已比旧CSV缓存更新，完整窗口数量可能不同。第一条可见状态或前一交易日缺失不能认作进入事件。", ""]
    table(lines, starting+[risk])
    lines += ["", f"有{len(unknown)}个守势日的前一交易日状态不可确认，未作为进入事件。", "", "## 情绪审核对照", ""]
    table(lines, sentiment)
    lines += ["", "当前市场方向规则不批准，情绪两条暂不批准；主线两条仍未启用。资金代理缺全历史板块明细，不能从这份大盘CSV补造，沿用档案模式积累。", "",
              "逐个进入日、温度事件、缺窗口状态与输入SHA256见同名JSON。两种回撤口径、每日状态与进入事件分别报告，不能把旧表37.1%贴作新规则命中率。", ""]
    atomic_text(output/"全历史回放.json", json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2)+"\n")
    atomic_text(output/"全历史回放.md", "\n".join(lines))
    print(f"全历史回放：市场{len(states)}日、温度{len(temps)}日；进入守势{risk['samples']}次；仅写{output}")
    return report
