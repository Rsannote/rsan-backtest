#!/usr/bin/env python3
"""更新行情库：按 data/标的清单.csv 抓取不复权日线和分红/复权表，算出后复权系数，存进 data/行情库/。

行情库是“活的”总库，每个交易日自动追加；回测不要直接读它，而是用 scripts/snapshot.py
从行情库切出一个固定的 data/日线_<日期>/ 目录再回测（那个目录一经使用就不再改动）。

存储（全部可离线重算）：
  data/行情库/<ETF|股票|指数>/<代码>.csv   列 date,open,high,low,close,volume,factor
      open/high/low/close/volume 是**不复权**原始值（腾讯 K 线），factor 是后复权系数（后复权价 = 原始价 × factor）。
  data/行情库/分红表/<代码>.json           新浪 hfq.js 的原始事件表
  data/行情库/状态.csv                      每个标的的起止日期、行数、最近分红、文件 sha256

复权方法（乘法复权，分红再投资）：
  股票：新浪 hfq.js 给出每个除权日的累计后复权因子 f，当天及以后用该 f；
  ETF ：新浪 hfq.js 给出累计每份分红 u、拆分比例 s，除息日 F *= 前收/(前收-每份分红)，拆分日 F *= s
        （与 scripts/fetch_etf.py 相同，2026-10-21 已核对与电脑数据一致）；
  指数：只有价格指数，factor=1。

用法：
  python3 scripts/update_hangqing.py                 # 增量：每个标的只重抓最近约 40 天，并和已存数据核对
  python3 scripts/update_hangqing.py --full          # 全部全量重抓
  python3 scripts/update_hangqing.py --full --rotate # 约四分之一全量重抓、其余增量（每周六自动跑，四周轮一遍，核对历史有没有被改）
  python3 scripts/update_hangqing.py --codes 600900,510880   # 只更新这几个（必须已在标的清单里）
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(ROOT, "data", "标的清单.csv")
LIB = os.path.join(ROOT, "data", "行情库")
EVD = os.path.join(LIB, "分红表")
UA_QQ = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}
UA_SINA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
COLS = ["open", "high", "low", "close", "volume"]
FULL_START = date(2005, 1, 1)
OVERLAP_DAYS = 40
PREFIX_TRIES = 3  # 首次建库的未知区间也先走限流退避，连续 501 才暂记无数据
PREFIX_RECHECK_DAYS = 10  # 按 T1 审核要求：首条距分段起点 10 个自然日内，向前核验
BJ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 网络
_lock = threading.Lock()
_next = [0.0]
PACE = 0.8  # 两次请求之间至少间隔的秒数（腾讯请求太密会返回 501）


def get(url, headers, tries=5, *, empty_501=False):
    last = None
    only_501 = True
    for i in range(tries):
        with _lock:  # 全局节流
            wait = _next[0] - time.time()
            if wait > 0:
                time.sleep(wait)
            _next[0] = time.time() + PACE
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            only_501 = only_501 and e.code == 501
            if e.code in (403, 429, 501, 503):  # 被限流：所有线程一起暂停
                with _lock:
                    _next[0] = time.time() + 30 * (i + 1)
            else:
                time.sleep(2 * (i + 1))
        except Exception as e:  # 超时、断连
            last = e
            only_501 = False
            time.sleep(2 * (i + 1))
    if empty_501 and only_501 and isinstance(last, urllib.error.HTTPError) and last.code == 501:
        raise last  # 仅连续 501 且退避尝试耗尽，才交给未知区间探测处理
    raise RuntimeError(f"请求失败：{url} -> {last!r}")


def symbol(code, typ):
    if typ == "指数":
        return code  # 清单里直接写 sh000300 / sz399006
    return ("sh" if code[0] in "569" else "sz") + code


def fetch_segment(sym, start, end, *, prefix=False):
    """请求一个分段；未知前缀的连续 501 经退避尝试耗尽后暂记为空。"""
    u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,"
         f"{start.isoformat()},{end.isoformat()},900,")
    try:
        text = get(u, UA_QQ, tries=PREFIX_TRIES, empty_501=True) if prefix else get(u, UA_QQ)
    except urllib.error.HTTPError as exc:
        if not (prefix and exc.code == 501):
            raise
        return {}, f"连续 {PREFIX_TRIES} 次尝试退避后仍返回 501"
    j = json.loads(text)
    if j.get("code", 0) != 0:
        raise RuntimeError(f"{sym} {start}—{end}：腾讯返回错误 {j.get('code')} {j.get('msg', '')}")
    node = (j.get("data") or {}).get(sym) or {}
    rows = {}
    for r in node.get("day") or []:
        if len(r) >= 6:
            rows[r[0]] = [float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5])]
    return rows, "腾讯空返回" if not rows else ""


def fetch_raw(sym, start, *, empty_prefix=False, notes=None):
    """腾讯不复权日线，按 3 年一段请求；可疑的前缀空段必须向前核验。"""
    rows = {}
    skipped = []
    end_all = date.today() + timedelta(days=1)
    s = start
    while s < end_all:
        e = min(date(s.year + 3, s.month, 1) - timedelta(days=1), end_all)
        prefix = empty_prefix and not rows
        part, reason = fetch_segment(sym, s, e, prefix=prefix)
        if prefix and not part:
            skipped.append((s, e))
            if notes is not None:
                notes.append(f"{sym} {s}—{e}：{reason}，暂记无数据并继续探测")
        rows.update(part)
        if prefix and rows:
            boundary = s
            # 补回的前一段若也紧贴边界，继续向前，不能只补一段又漏更早历史。
            while skipped and (date.fromisoformat(min(rows)) - boundary).days <= PREFIX_RECHECK_DAYS:
                previous_start, previous_end = skipped.pop()
                if notes is not None:
                    notes.append(f"{sym} 首条数据紧贴分段起点，重新核验 {previous_start}—{previous_end}")
                recovered, why = fetch_segment(sym, previous_start, previous_end, prefix=True)
                if not recovered:
                    raise RuntimeError(f"{sym} {previous_start}—{previous_end} 前缀重探仍无法确认历史是否完整（{why}），不写入")
                rows.update(recovered)
                boundary = previous_start
        s = e + timedelta(days=1)
    # 腾讯日线顺序是 日期,开,收,高,低,量 → 这里存成 开,高,低,收,量
    df = pd.DataFrame([[d] + v for d, v in sorted(rows.items())], columns=["date"] + COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def fetch_full(sym, path, notes):
    """已有腾讯日线从首个数据年份开始；首次建库允许跳过上市前的空区间。"""
    previous = read_store(path)
    if previous is not None and len(previous):
        first = previous.index.min().date()
        start = max(FULL_START, date(first.year, 1, 1))
        notes.append(f"{sym} 全量起点 {start}：取已存腾讯日线首年（首日 {first}），未手填上市日期")
        # 已知首年应当有数据，501 必须按请求失败处理，防止丢失历史。
        return fetch_raw(sym, start)
    notes.append(f"{sym} 未取得首个数据年份，从 {FULL_START} 分段探测；前缀 501 先退避重试，首条贴近边界则向前核验")
    return fetch_raw(sym, FULL_START, empty_prefix=True, notes=notes)


def fetch_events(sym):
    t = get(f"https://finance.sina.com.cn/realstock/company/{sym}/hfq.js", UA_SINA)
    m = re.search(r"=\s*(\{.*?\})\s*(?:/\*|$)", t, re.S)
    if not m:
        raise RuntimeError(f"{sym}: 新浪 hfq.js 格式解析失败：{t[:120]!r}")
    data = json.loads(m.group(1)).get("data") or []
    return sorted((x for x in data if not str(x.get("d", "")).startswith("1900")), key=lambda x: x["d"])


# ---------------------------------------------------------------- 计算
def drop_spikes(raw, code, notes):
    """孤立坏点：当天收盘与前后两天都相差 40% 以上，而前后两天彼此相差不到 15%。"""
    pc, nc = raw["close"].shift(1), raw["close"].shift(-1)
    spike = (((raw["close"] / pc - 1).abs() > 0.4) & ((raw["close"] / nc - 1).abs() > 0.4)
             & ((pc / nc - 1).abs() < 0.15)).fillna(False)
    for d in raw.index[spike]:
        notes.append(f"{code} {d.date()} 孤立坏点已剔除：open={raw.at[d, 'open']} close={raw.at[d, 'close']}（前收 {pc.at[d]}，后收 {nc.at[d]}）")
    return raw[~spike]


def factors(raw, events, typ):
    cal = raw.index
    F = pd.Series(1.0, index=cal)
    if typ == "指数" or not events:
        return F
    if "u" not in events[0]:  # 股票：只有累计因子 f（ETF 的表里也有 f，但恒为 1，要用 u/s）
        ev = pd.Series([float(x["f"]) for x in events], index=pd.to_datetime([x["d"] for x in events]))
        ev = ev[~ev.index.duplicated(keep="last")].sort_index()
        pos = ev.index.searchsorted(cal, side="right") - 1
        vals = [ev.iloc[p] if p >= 0 else 1.0 for p in pos]
        # 上市前就有的事件（因子 >1 但早于本库第一天）不会出现；上市当天之前一律按 1
        return pd.Series(vals, index=cal, dtype=float)
    # ETF：u = 累计每份分红，s = 累计份额折算比例（两者都是累计值，相邻事件相减/相除得到当次）。
    # 折算的生效日：新浪有时把登记日和实际折算日都列出来，所以在事件日起 4 个交易日内，
    # 选开盘/前收最接近 1/当次比例 的那天生效。
    by_day, prev_u, prev_s = {}, 0.0, 1.0
    opens, closes = raw["open"].values, raw["close"].values
    for x in events:
        u, s = float(x.get("u", 0) or 0), float(x.get("s", 1) or 1)
        div = round(u - prev_u, 6)
        ratio = s / prev_s if s > 0 else 1.0
        prev_u, prev_s = u, (s if s > 0 else prev_s)
        p = cal.searchsorted(pd.Timestamp(x["d"]))  # 除息日不是交易日时顺延
        if not 0 < p < len(cal):
            continue
        if div > 0:
            by_day.setdefault(p, []).append(("div", div))
        if abs(ratio - 1) > 0.01:
            cands = range(p, min(p + 4, len(cal)))
            best = min(cands, key=lambda k: abs(np.log(opens[k] / closes[k - 1]) + np.log(ratio)))
            by_day.setdefault(best, []).append(("split", ratio))
    f = 1.0
    out = []
    for i in range(len(cal)):
        for kind, v in by_day.get(i, []):
            if kind == "div":
                f *= closes[i - 1] / (closes[i - 1] - v)
            else:
                f *= v
        out.append(f)
    return pd.Series(out, index=cal)


def read_store(path):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df


def write_store(path, df):
    out = df.copy()
    for c in ["open", "high", "low", "close"]:
        out[c] = out[c].round(4)
    out["volume"] = out["volume"].round(2)
    out["factor"] = out["factor"].round(10)
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    out[COLS + ["factor"]].to_csv(path, float_format="%.10g")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# ---------------------------------------------------------------- 单个标的
def update_one(item, full):
    code, typ, name = item["code"], item["type"], item["name"]
    sym = symbol(code, typ)
    path = os.path.join(LIB, typ, f"{code}.csv")
    evp = os.path.join(EVD, f"{code}.json")
    rep = {"code": code, "name": name, "type": typ, "notes": [], "status": "ok"}
    old = None if full else read_store(path)
    try:
        if old is not None and len(old):
            start = (old.index[-1] - pd.Timedelta(days=OVERLAP_DAYS)).date()
            tail = fetch_raw(sym, start)
            ov = old.index.intersection(tail.index)
            mism = [d for d in ov if any(abs(old.at[d, c] - tail.at[d, c]) > 1e-6 * max(1, abs(tail.at[d, c]))
                                         for c in ["open", "close"])]
            if len(tail) == 0:
                raise RuntimeError("腾讯没有返回最近的数据")
            if mism or len(ov) == 0:
                rep["notes"].append(f"最近数据与已存不一致（{len(mism)} 天，如 {mism[:3]}），改为全量重抓")
                raw = fetch_full(sym, path, rep["notes"])
            else:
                raw = pd.concat([old.loc[old.index < tail.index[0], COLS], tail[COLS]])
        else:
            raw = fetch_full(sym, path, rep["notes"])
        if len(raw) == 0:
            raise RuntimeError("腾讯没有返回任何数据")
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()
        raw = raw[(raw["open"] > 0) & (raw["close"] > 0)]
        raw = drop_spikes(raw, code, rep["notes"])

        events = []
        if typ != "指数":
            old_ev = json.load(open(evp, encoding="utf-8")) if os.path.exists(evp) else None
            try:
                events = fetch_events(sym)
            except Exception as e:
                if old_ev is None:
                    raise
                events = old_ev
                rep["notes"].append(f"新浪分红表获取失败，沿用已存的分红表：{e}")
            if old_ev is not None:
                new = [x for x in events if x not in old_ev]
                gone = [x for x in old_ev if x not in events]
                if new:
                    rep["notes"].append(f"新的分红/除权事件：{[x['d'] for x in new]}")
                if gone:
                    rep["notes"].append(f"⚠️ 分红表里有旧事件被修改或删除：{[x['d'] for x in gone]}")
        raw["factor"] = factors(raw, events, typ)

        # 自检：后复权开盘相对前一天后复权收盘跳空超过 21%
        adj_o, adj_c = raw["open"] * raw["factor"], raw["close"] * raw["factor"]
        gap = (adj_o / adj_c.shift(1) - 1).abs()
        big = gap[gap > 0.21]
        if len(big):
            rep["gaps"] = [str(d.date()) for d in big.index]

        # 和旧文件比：历史行有没有被改动（不只是追加）
        prev = read_store(path)
        if prev is not None:
            common = prev.index.intersection(raw.index)
            chg = [d for d in common
                   if abs(prev.at[d, "close"] * prev.at[d, "factor"] - raw.at[d, "close"] * raw.at[d, "factor"])
                   > 1e-6 * raw.at[d, "close"] * raw.at[d, "factor"]]
            lost = prev.index.difference(raw.index)
            if chg:
                rep["notes"].append(f"⚠️ {len(chg)} 个历史交易日的后复权价发生变化（首个 {chg[0].date()}）")
            if len(lost):
                rep["notes"].append(f"⚠️ {len(lost)} 个历史交易日被删除（如 {[str(d.date()) for d in lost[:3]]}）")
            if len(raw) < 0.9 * len(prev):
                raise RuntimeError(f"新数据只有 {len(raw)} 行，比已存的 {len(prev)} 行少太多，不覆盖")
            rep["added"] = int(len(raw.index.difference(prev.index)))
        else:
            rep["added"] = len(raw)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_store(path, raw)
        if typ != "指数":
            os.makedirs(EVD, exist_ok=True)
            with open(evp, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=0)
        rep.update(first=str(raw.index[0].date()), last=str(raw.index[-1].date()), rows=len(raw),
                   events=len(events), last_event=(events[-1]["d"] if events else ""),
                   factor_last=round(float(raw["factor"].iloc[-1]), 6), sha256=sha(path)[:16])
    except Exception as e:
        rep["status"] = "失败"
        rep["notes"].append(f"{type(e).__name__}: {e}"[:300])
    return rep


# ---------------------------------------------------------------- 主程序
def retry_worker(config_path, result_path):
    """隔离子进程只调用原单标的逻辑，不直接读写正式库。"""
    global LIB, EVD, PACE
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    LIB = config["library"]
    EVD = os.path.join(LIB, "分红表")
    PACE = config["pace"]
    _next[0] = config.get("next_request_at", 0.0)
    rep = update_one(config["item"], config["full"])
    if rep["status"] == "ok" and config["item"]["type"] != "指数":
        rep["events_sha256"] = sha(os.path.join(EVD, config["item"]["code"] + ".json"))
    Path(result_path).write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    return 0


def validate_retry_output(library, item, rep):
    """晋升前核对完整文件及原单标的报告，坏/半写结果绝不回库。"""
    price = Path(library) / item["type"] / (item["code"] + ".csv")
    raw = read_store(price)
    if raw is None or list(raw.columns) != COLS + ["factor"] or not len(raw):
        raise ValueError("补抓CSV缺失、为空或列结构错误")
    if raw.index.hasnans or not raw.index.is_unique or not raw.index.is_monotonic_increasing:
        raise ValueError("补抓CSV日期缺失、重复或未排序")
    if not np.isfinite(raw.to_numpy(dtype=float)).all() or (raw[["open", "close", "factor"]] <= 0).any().any():
        raise ValueError("补抓CSV含无效价格/复权因子")
    if (len(raw) != rep["rows"] or str(raw.index[0].date()) != rep["first"]
            or str(raw.index[-1].date()) != rep["last"] or sha(price)[:16] != rep["sha256"]
            or not math.isclose(float(raw["factor"].iloc[-1]), rep["factor_last"], rel_tol=1e-9, abs_tol=5.001e-7)):
        raise ValueError("补抓CSV行数、日期或SHA与报告不一致")
    files = [price]
    if item["type"] != "指数":
        events_path = Path(library) / "分红表" / (item["code"] + ".json")
        with events_path.open(encoding="utf-8") as f:
            events = json.load(f)
        if not isinstance(events, list) or any(not isinstance(e, dict) or not isinstance(e.get("d"), str) for e in events):
            raise ValueError("补抓分红表结构错误")
        for event in events:
            date.fromisoformat(event["d"])
        if (len(events) != rep["events"] or (events[-1]["d"] if events else "") != rep["last_event"]
                or sha(events_path) != rep["events_sha256"]):
            raise ValueError("补抓分红表数量、末次日期或SHA与报告不一致")
        files.append(events_path)
    return files


def promote_retry_files(library, files):
    """先备齐临时文件再逐个原子替换；晋升异常时回滚已替换文件。"""
    prepared, replaced = [], []
    try:
        for source in files:
            target = Path(LIB) / source.relative_to(library)
            target.parent.mkdir(parents=True, exist_ok=True)
            def temporary():
                fd, name = tempfile.mkstemp(prefix=".t11-", dir=target.parent)
                os.close(fd)
                return Path(name)
            incoming = temporary()
            backup = temporary() if target.exists() else None
            prepared.append((target, incoming, backup))
            shutil.copyfile(source, incoming)
            if backup:
                shutil.copyfile(target, backup)
        for target, incoming, backup in prepared:
            os.replace(incoming, target)
            replaced.append((target, backup))
    except BaseException:
        for target, backup in reversed(replaced):
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(backup, target)
        raise
    finally:
        for _, incoming, backup in prepared:
            incoming.unlink(missing_ok=True)
            if backup:
                backup.unlink(missing_ok=True)


def retry_attempt(item, full, timeout, pace):
    """父进程硬截子进程；被kill的进程仅可能污染临时副本。"""
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="hangqing-retry-") as folder:
        library = Path(folder) / "行情库"
        for relative in (Path(item["type"]) / (item["code"] + ".csv"), Path("分红表") / (item["code"] + ".json")):
            source, target = Path(LIB) / relative, library / relative
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        config, result = Path(folder) / "config.json", Path(folder) / "result.json"
        config.write_text(json.dumps({"item": item, "full": full, "library": str(library), "pace": pace,
                                      "next_request_at": _next[0]}, ensure_ascii=False), encoding="utf-8")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("隔离副本准备后已到单标的预算")
        try:
            process = subprocess.run([sys.executable, os.path.abspath(__file__), "--retry-worker", str(config), str(result)],
                                     timeout=remaining, capture_output=True, text=True)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"单标的补抓达到 {timeout:g} 秒硬截止，子进程已终止，未晋升临时数据") from exc
        if process.returncode != 0 or not result.is_file():
            raise RuntimeError(f"补抓子进程异常退出 {process.returncode}：{process.stderr[-300:]}")
        rep = json.loads(result.read_text(encoding="utf-8"))
        if rep.get("status") == "ok":
            if any(rep.get(k) != item[k] for k in ("code", "name", "type")):
                raise ValueError("补抓报告标的与请求不一致")
            files = validate_retry_output(library, item, rep)
            if time.monotonic() >= deadline:
                raise TimeoutError("补抓结果校验后已到单标的预算，未晋升临时数据")
            promote_retry_files(library, files)
        return rep


def retry_failed(items, reps, *, rounds=2, wait=60, pace=2, budget=600, item_timeout=120,
                 attempt=None, clock=None, sleep=None):
    """只重试本次明确失败项；单线程、最多两轮，等待也计入600秒预算。"""
    attempt, clock, sleep = attempt or retry_attempt, clock or time.monotonic, sleep or time.sleep
    start = clock()
    metadata = {"initial_failed": sum(r["status"] == "失败" for r in reps), "recovered": [], "attempts": 0,
                "rounds": 0, "budget_exhausted": False, "budget_seconds": budget}
    by_code = {item["code"]: item for item in items}
    for round_number in range(1, rounds + 1):
        pending = [index for index, rep in enumerate(reps) if rep["status"] == "失败"]
        if not pending or clock() - start >= budget:
            break
        sleep(min(wait, max(0, budget - (clock() - start))))
        if clock() - start >= budget:
            break
        metadata["rounds"] = round_number
        for position, index in enumerate(pending):
            if position:
                sleep(min(pace, max(0, budget - (clock() - start))))
            remaining = budget - (clock() - start)
            if remaining <= 0:
                break
            previous = reps[index]
            metadata["attempts"] += 1
            try:
                rep = attempt(by_code[previous["code"]], previous["full"], min(item_timeout, remaining), pace)
            except Exception as exc:
                rep = {"status": "失败", "notes": [f"{type(exc).__name__}: {exc}"]}
            if rep.get("status") == "ok":
                reps[index] = rep | {"full": previous["full"], "retry_round": round_number}
                metadata["recovered"].append({"code": previous["code"], "name": previous["name"], "round": round_number,
                                               "full": previous["full"]})
            else:
                previous["notes"].append(f"补抓第 {round_number} 轮失败：{'；'.join(rep.get('notes', ['返回状态不可用']))}")
    metadata["elapsed_seconds"] = max(0, clock() - start)
    metadata["budget_exhausted"] = metadata["initial_failed"] > len(metadata["recovered"]) and metadata["elapsed_seconds"] >= budget
    if metadata["budget_exhausted"]:
        for rep in reps:
            if rep["status"] == "失败":
                rep["notes"].append(f"补抓阶段 {budget:g} 秒总预算已用尽，保留失败；下次正常运行继续处理")
    return metadata


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量重抓")
    ap.add_argument("--codes", default="", help="只更新这些代码，逗号分隔")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--budget", type=float, default=60, help="最多用多少分钟；超时后不再开始新的标的（没轮到的下次自动补上）")
    ap.add_argument("--rotate", action="store_true", help="配合 --full：只全量重抓约四分之一的标的（按周轮换，四周一轮），其余增量")
    ap.add_argument("--retry-rounds", type=int, choices=(0, 1, 2), default=2, help="失败补抓轮数，最多2轮，0为禁用")
    ap.add_argument("--retry-wait", type=float, default=60, help="每轮补抓前等待秒数（草案60秒，计入补抓总预算）")
    ap.add_argument("--retry-pace", type=float, default=2, help="补抓请求及标的间隔秒数，须慢于主循环（草案2秒）")
    ap.add_argument("--retry-budget", type=float, default=600, help="补抓额外总预算秒数，含等待，最多600秒")
    ap.add_argument("--retry-item-timeout", type=float, default=120, help="单标的补抓硬截止秒数（草案120秒，且不超过剩余总预算）")
    ap.add_argument("--retry-worker", nargs=2, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.retry_worker:
        return retry_worker(*a.retry_worker)
    for key in ("budget", "retry_wait", "retry_pace", "retry_budget", "retry_item_timeout"):
        value = getattr(a, key)
        if not math.isfinite(value) or value < 0:
            ap.error(f"{key}须为有限非负数")
    if a.workers < 1 or a.budget <= 0 or a.retry_item_timeout <= 0:
        ap.error("workers、budget与retry-item-timeout必须大于0")
    if a.retry_budget > 600 or a.retry_pace <= PACE:
        ap.error("补抓总预算不得超过600秒，retry-pace须慢于主循环0.8秒")

    items = list(csv.DictReader(open(LIST, encoding="utf-8")))
    if a.codes:
        want = set(a.codes.split(","))
        miss = want - {x["code"] for x in items}
        if miss:
            sys.exit(f"这些代码不在 data/标的清单.csv 里，请先加进清单：{sorted(miss)}")
        items = [x for x in items if x["code"] in want]

    t0 = time.time()
    # 先做还没有文件的（新标的），再按上次更新时间从旧到新，保证超时没轮到的下次优先
    stp0 = os.path.join(LIB, "状态.csv")
    last_upd = {}
    if os.path.exists(stp0):
        last_upd = {r["code"]: r.get("updated", "") for r in csv.DictReader(open(stp0, encoding="utf-8"))}
    items.sort(key=lambda it: last_upd.get(it["code"], ""))
    week = date.today().isocalendar()[1]
    def is_full(it):
        if not a.full:
            return False
        if not a.rotate:
            return True
        return int(hashlib.md5(it["code"].encode()).hexdigest(), 16) % 4 == week % 4
    with ThreadPoolExecutor(a.workers) as ex:
        def job(it):
            if time.time() - t0 > a.budget * 60:
                return {"code": it["code"], "name": it["name"], "type": it["type"], "status": "未轮到",
                        "notes": [f"超过 {a.budget:.0f} 分钟时间预算，下次运行补上"], "full": False}
            return update_one(it, is_full(it)) | {"full": is_full(it)}
        reps = list(ex.map(job, items))

    retry = retry_failed(items, reps, rounds=a.retry_rounds, wait=a.retry_wait, pace=a.retry_pace,
                         budget=a.retry_budget, item_timeout=a.retry_item_timeout)

    # 状态表：保留没在本次更新范围里的旧行
    stp = os.path.join(LIB, "状态.csv")
    keep = {}
    if os.path.exists(stp):
        for r in csv.DictReader(open(stp, encoding="utf-8")):
            keep[r["code"]] = r
    now = datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    fields = ["code", "name", "type", "first", "last", "rows", "events", "last_event", "factor_last", "sha256", "updated"]
    for r in reps:
        if r["status"] == "ok":
            keep[r["code"]] = {k: r.get(k, "") for k in fields} | {"updated": now}
    order = [x["code"] for x in csv.DictReader(open(LIST, encoding="utf-8"))]
    with open(stp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in order:
            if c in keep:
                w.writerow({k: keep[c].get(k, "") for k in fields})

    ok = [r for r in reps if r["status"] == "ok"]
    bad = [r for r in reps if r["status"] == "失败"]
    skipped = [r for r in reps if r["status"] == "未轮到"]
    lasts = sorted({r["last"] for r in ok})
    lines = [f"# 行情库更新 {now}（北京时间）", "",
             f"- 模式：全量重抓 {sum(r['full'] for r in reps)} 个，其余增量；标的 {len(reps)} 个，成功 {len(ok)}，失败 {len(bad)}，超时未轮到 {len(skipped)}；用时 {time.time() - t0:.0f} 秒",
             f"- 最新交易日：{lasts[-1] if lasts else '无'}" + (f"（有 {sum(r['last'] != lasts[-1] for r in ok)} 个标的最后日期更早，可能停牌）" if len(lasts) > 1 else ""),
             f"- 新增行数合计：{sum(r.get('added', 0) for r in ok)}", ""]
    lines += [f"- 主循环失败：{retry['initial_failed']}；补抓成功：{len(retry['recovered'])}；补抓轮数：{retry['rounds']}；补抓请求标的次数：{retry['attempts']}；额外用时 {retry['elapsed_seconds']:.2f} 秒（上限 {retry['budget_seconds']:g} 秒，含等待）", ""]
    if retry["recovered"]:
        lines += ["## 补抓成功", ""] + [f"- {r['code']} {r['name']}：第 {r['round']} 轮，{'全量' if r['full'] else '增量'}" for r in retry["recovered"]] + [""]
    if bad:
        lines += ["## 失败", ""] + [f"- {r['code']} {r['name']}：{'；'.join(r['notes'])}" for r in bad] + [""]
    if skipped:
        lines += [f"## 超时未轮到（下次运行优先补上）", "", "- " + "、".join(f"{r['code']} {r['name']}" for r in skipped), ""]
    noted = [r for r in ok if r["notes"]]
    if noted:
        lines += ["## 需要留意", ""] + [f"- {r['code']} {r['name']}：{'；'.join(r['notes'])}" for r in noted] + [""]
    gapped = [r for r in ok if r.get("gaps")]
    if gapped:
        lines += ["## 后复权跳空超过 21% 的交易日（多为长期停牌复牌或上市首日，复权有误时也会出现）", ""]
        lines += [f"- {r['code']} {r['name']}：{len(r['gaps'])} 天，{r['gaps'][:6]}" for r in gapped] + [""]
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    text = "\n".join(lines)
    with open(os.path.join(ROOT, "logs", "行情库_最新.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 1 if len(bad) > max(2, 0.1 * len(reps)) else 0


if __name__ == "__main__":
    sys.exit(main())
