#!/usr/bin/env python3
"""Rsan 固定口径回测引擎。

口径（写死，改口径就改这里并升版本号）：
- 价格：后复权日线，分红再投资（数据由调用方保证已复权，乘法复权）
- 成交：第 t 日收盘后出信号，第 t+1 日开盘价成交；先卖后买；100 股一手
- 一手按真实股价计算（v1.1）：CSV 需带 factor 列（=后复权价/真实价）；缺 factor 的标的按后复权价取整，并在报告里报警
- 本金：必须用 --capital 写明（v1.1 起无默认值），报告里写明本金
- 复现：每次运行把引擎、策略文件夹、数据清单（逐文件 sha256）、命令行存进输出目录（v1.1）
- 自检：报告列出每个策略的平均现金占比，超过 10% 报警（防止现金闲置的写法错误）（v1.1）
- 佣金：万一，免五，单笔最低 0.1 元
- 印花税：股票卖出 0.05%；ETF 免
- 过户费：沪市股票双向 0.001%；ETF 免
- 现金：不计利息
- 基准：红利 510880 买入持有；红利稳健配置 510880/513100/511260/518880 = 30/30/20/20，季度再平衡
- 窗口：策略与两个基准使用完全相同的起止日期和交易日历
- 淘汰：策略年化 <= 红利年化 -> 淘汰
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ENGINE_VERSION = "1.1"
CASH_WARN = 0.10  # 平均现金占比超过这个值就报警
COMMISSION_RATE = 0.0001
COMMISSION_MIN = 0.1
STAMP_DUTY_SELL = 0.0005
TRANSFER_FEE_SH = 0.00001
LOT = 100
DIVIDEND_CODE = "510880"
CPLAN = {"510880": 0.30, "513100": 0.30, "511260": 0.20, "518880": 0.20}
ETF_PREFIXES = ("15", "16", "18", "50", "51", "52", "53", "56", "58")


# ---------------------------------------------------------------- data
def norm_code(name: str) -> str:
    s = "".join(ch for ch in name if ch.isdigit())
    return s[-6:] if len(s) >= 6 else name


def is_etf(code: str) -> bool:
    return code.startswith(ETF_PREFIXES)


def is_sh_stock(code: str) -> bool:
    return code.startswith("6") and not is_etf(code)


COLMAP = {
    "日期": "date", "交易日期": "date", "时间": "date", "trade_date": "date",
    "开盘": "open", "开盘价": "open", "收盘": "close", "收盘价": "close",
    "最高": "high", "最高价": "high", "最低": "low", "最低价": "low",
    "成交量": "volume", "vol": "volume", "成交额": "amount",
}


def load_price_file(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "gbk"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    df.columns = [COLMAP.get(str(c).strip(), str(c).strip().lower()) for c in df.columns]
    for c in ("date", "open", "close"):
        if c not in df.columns:
            raise ValueError(f"{path.name}: 缺少列 {c}（需要 date/open/close，可选 high/low/volume）")
    df["date"] = pd.to_datetime(df["date"].astype(str), format="mixed")
    df = df.sort_values("date").drop_duplicates("date").set_index("date")
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "close"])
    df = df[(df["open"] > 0) & (df["close"] > 0)]
    if "factor" in df.columns:
        df["factor"] = df["factor"].where(df["factor"] > 0).ffill().bfill()
    return df


def load_dir(data_dir: Path) -> dict[str, pd.DataFrame]:
    out = {}
    for p in sorted(data_dir.glob("*.csv")):
        out[norm_code(p.stem)] = load_price_file(p)
    if not out:
        raise SystemExit(f"{data_dir} 下没有 csv 文件")
    return out


def data_warnings(prices: dict[str, pd.DataFrame]) -> list[str]:
    warns = []
    for code, df in prices.items():
        gap = (df["open"] / df["close"].shift(1) - 1).abs()
        big = gap[gap > 0.21]
        if len(big):
            d = big.index[0].date()
            warns.append(f"{code}: {len(big)} 个交易日开盘相对前收跳空超过 21%（首次 {d}），疑似未复权，请确认是后复权数据")
    return warns


# ---------------------------------------------------------------- engine
@dataclass
class Ctx:
    date: pd.Timestamp
    _prices: dict
    positions: dict
    cash: float
    nav: float
    extra: dict
    state: dict

    def history(self, code: str, n: int | None = None) -> pd.DataFrame:
        """截至今日收盘（含今日）的历史，不含任何未来数据。"""
        df = self._prices.get(code)
        if df is None:
            return pd.DataFrame()
        h = df.loc[: self.date]
        return h.tail(n) if n else h

    def close(self, code: str) -> float | None:
        h = self.history(code, 1)
        return None if h.empty else float(h["close"].iloc[-1])

    def real_price(self, code: str) -> float | None:
        """今日收盘的真实（不复权）价格，用于判断一手要多少钱；没有 factor 时返回后复权价。"""
        h = self.history(code, 1)
        if h.empty:
            return None
        f = float(h["factor"].iloc[-1]) if "factor" in h.columns else 1.0
        return float(h["close"].iloc[-1]) / (f or 1.0)

    @property
    def universe(self) -> list[str]:
        return [c for c, df in self._prices.items() if len(df.loc[: self.date])]

    def weights(self) -> dict:
        w = {}
        for c, sh in self.positions.items():
            px = self.close(c)
            if px and sh:
                w[c] = sh * px / self.nav
        return w


@dataclass
class Result:
    name: str
    nav: pd.Series
    trades: pd.DataFrame
    fees: float
    turnover: float
    notes: list = field(default_factory=list)


def fee_for(code: str, side: str, amount: float) -> float:
    fee = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    if not is_etf(code):
        if side == "sell":
            fee += amount * STAMP_DUTY_SELL
        if is_sh_stock(code):
            fee += amount * TRANSFER_FEE_SH
    return fee


def period_end_flags(cal: pd.DatetimeIndex, freq: str) -> np.ndarray:
    if freq == "daily":
        return np.ones(len(cal), bool)
    key = {"weekly": cal.to_period("W"), "monthly": cal.to_period("M"),
           "quarterly": cal.to_period("Q"), "yearly": cal.to_period("Y")}[freq]
    s = pd.Series(key, index=cal)
    return (s != s.shift(-1)).to_numpy()


def run(name, decide, prices, cal, capital, extra, rebalance="daily", min_trade_frac=0.0):
    # 持仓 pos 以“后复权股数”记账（股数×后复权价=市值）；一手的判断换算成真实股数：真实股数 = 后复权股数 × factor
    cash = capital
    pos: dict[str, float] = {}
    pending: dict | None = None
    navs, cash_ratio, trades = [], [], []
    fees = traded = 0.0
    state: dict = {}
    flags = period_end_flags(cal, rebalance)
    notes = []
    no_factor = set()

    def px(code, d, col):
        df = prices.get(code)
        if df is None or d not in df.index:
            return None
        return float(df.at[d, col])

    def factor(code, d):
        df = prices.get(code)
        if df is None or "factor" not in df.columns:
            no_factor.add(code)
            return 1.0
        f = px(code, d, "factor")
        return f if f and f > 0 else 1.0

    last_close: dict[str, float] = {}
    for i, d in enumerate(cal):
        # 1) 执行昨日信号：今日开盘成交
        if pending is not None:
            nav_open = cash + sum(sh * (px(c, d, "open") or last_close.get(c, 0)) for c, sh in sorted(pos.items()))
            targets = {}  # 目标真实股数（整手）
            for c, w in pending.items():
                o = px(c, d, "open")
                if o is None:
                    continue
                raw = o / factor(c, d)
                targets[c] = int(nav_open * w / raw // LOT) * LOT
            orders = []
            for c in sorted(set(pos) | set(targets)):
                o = px(c, d, "open")
                if o is None:  # 停牌/未上市：维持原状
                    continue
                fc = factor(c, d)
                cur_real = pos.get(c, 0.0) * fc
                tgt = targets.get(c, 0)
                if tgt == 0:
                    delta_real = -cur_real  # 清仓：全部卖出（含分红再投资产生的零股）
                else:
                    delta_real = round((tgt - cur_real) / LOT) * LOT
                if abs(delta_real) < 1e-9:
                    continue
                if min_trade_frac and tgt and cur_real and abs(delta_real * o / fc) < min_trade_frac * nav_open:
                    continue
                orders.append((c, delta_real, o, fc))
            for c, delta_real, o, fc in sorted(orders, key=lambda x: (x[1], x[0])):  # 先卖后买；同额按代码排序，保证每次结果逐位一致
                raw = o / fc
                if delta_real > 0:
                    afford = int((cash - COMMISSION_MIN) / (raw * (1 + COMMISSION_RATE + TRANSFER_FEE_SH)) // LOT) * LOT
                    delta_real = min(delta_real, max(afford, 0))
                    if delta_real <= 0:
                        continue
                else:
                    delta_real = max(delta_real, -pos.get(c, 0.0) * fc)
                delta = delta_real / fc  # 换回后复权股数
                amt = abs(delta) * o
                side = "buy" if delta > 0 else "sell"
                f = fee_for(c, side, amt)
                cash += -amt - f if side == "buy" else amt - f
                pos[c] = pos.get(c, 0.0) + delta
                if abs(pos[c] * fc) < 0.5:
                    del pos[c]
                fees += f
                traded += amt
                trades.append({"date": d.date(), "code": c, "side": side, "real_shares": round(abs(delta_real)),
                               "real_price": round(raw, 4), "amount": round(amt, 2), "fee": round(f, 2)})
            if cash < -1e-6:
                raise RuntimeError(f"{name} {d.date()} 现金为负 {cash:.2f}，引擎错误")
            pending = None
        # 2) 收盘估值
        for c in pos:
            cl = px(c, d, "close")
            if cl is not None:
                last_close[c] = cl
        nav = cash + sum(sh * last_close.get(c, 0) for c, sh in sorted(pos.items()))
        navs.append(nav)
        cash_ratio.append(cash / nav if nav > 0 else 0.0)
        # 3) 收盘后决策（最后一天不再决策）
        if i < len(cal) - 1 and (flags[i] or i == 0):
            ctx = Ctx(d, prices, dict(pos), cash, nav, extra, state)
            w = decide(ctx)
            if w is not None:
                w = {norm_code(str(k)): float(v) for k, v in w.items() if v and v > 0}
                tot = sum(w.values())
                if tot > 1.0001:
                    w = {k: v / tot for k, v in w.items()}
                    if not any("权重之和" in n for n in notes):
                        notes.append("策略某些日期给出的权重之和 >1，已按比例缩放（不支持杠杆）")
                pending = w
    nav = pd.Series(navs, index=cal, name=name)
    if no_factor:
        notes.append(f"{name}：{len(no_factor)} 个标的没有 factor 列（真实价），一手按后复权价取整，结果会偏差："
                     + "、".join(sorted(no_factor)[:10]))
    res = Result(name, nav, pd.DataFrame(trades), fees, traded, notes)
    res.cash_ratio = pd.Series(cash_ratio, index=cal)
    return res


# ---------------------------------------------------------------- metrics
def metrics(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    total = nav.iloc[-1] / nav.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else float("nan")
    dd = nav / nav.cummax() - 1
    mdd = dd.min()
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    rec = nav.loc[trough:]
    rec = rec[rec >= nav.loc[peak]]
    vol = r.std() * math.sqrt(252)
    sharpe = (r.mean() * 252 - 0.02) / vol if vol > 0 else float("nan")
    return {
        "总收益": total, "年化": cagr, "最大回撤": mdd,
        "回撤区间": f"{peak.date()} → {trough.date()}" + (f"，{rec.index[0].date()} 修复" if len(rec) else "，未修复"),
        "年化波动": vol, "夏普(rf=2%)": sharpe, "卡玛": cagr / abs(mdd) if mdd < 0 else float("nan"),
    }


def yearly(nav: pd.Series) -> pd.Series:
    ye = nav.groupby(nav.index.year).last()
    first = nav.iloc[0]
    prev = pd.concat([pd.Series([first], index=[ye.index[0] - 1]), ye.iloc[:-1]])
    prev.index = ye.index
    return ye / prev - 1


def pct(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x*100:.2f}%"


# ---------------------------------------------------------------- strategies
def load_strategy(path: Path):
    spec = importlib.util.spec_from_file_location("user_strategy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "target_weights"):
        raise SystemExit(f"{path} 必须定义 target_weights(ctx)")
    return mod


def hold_dividend(ctx):
    return {DIVIDEND_CODE: 1.0}


def cplan(ctx):
    return dict(CPLAN)


# ---------------------------------------------------------------- reproducibility
def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def archive_inputs(out: Path, a, engine_path: Path) -> dict:
    """把引擎、策略文件夹（.py/.json/.csv，单个≤5MB）、数据清单和命令行存进 out/repro/。"""
    rp = out / "repro"
    (rp / "strategies").mkdir(parents=True)
    shutil.copy2(engine_path, rp / "backtest.py")
    copied = {}
    for s in a.strategy:
        sdir = Path(s).resolve().parent
        if sdir in copied:
            continue
        dst = rp / "strategies" / (sdir.name or "root")
        dst.mkdir(exist_ok=True)
        for p in sdir.iterdir():
            if p.is_file() and p.suffix in (".py", ".json", ".csv") and p.stat().st_size <= 5 << 20:
                shutil.copy2(p, dst / p.name)
        copied[sdir] = dst
    rows = [(p.name, sha(p)) for p in sorted(Path(a.data).glob("*.csv"))]
    pd.DataFrame(rows, columns=["file", "sha256"]).to_csv(rp / "data_manifest.csv", index=False)
    data_hash = hashlib.sha256("".join(f"{n}:{h};" for n, h in rows).encode()).hexdigest()[:16]
    strat_args = " ".join(f'"repro/strategies/{copied[Path(s).resolve().parent].name}/{Path(s).name}"' for s in a.strategy)
    rel = lambda p: os.path.relpath(Path(p).resolve(), out.resolve())  # 相对路径：换电脑/换挂载点也能复现
    cmd = (f'python3 repro/backtest.py --data "{rel(a.data)}" --strategy {strat_args} '
           f'--capital {a.capital:.0f} --out rerun' + (f" --start {a.start}" if a.start else "")
           + (f" --end {a.end}" if a.end else "") + (f' --extra "{rel(a.extra)}"' if a.extra else ""))
    (rp / "rerun.sh").write_text("#!/bin/bash\n# 原样复现本次回测，结果写到 rerun/\ncd \"$(dirname \"$0\")/..\"\n" + cmd + "\n", encoding="utf-8")
    (rp / "command.txt").write_text(" ".join(sys.argv), encoding="utf-8")
    return {"engine_hash": sha(engine_path)[:16], "data_hash": data_hash, "n_files": len(rows)}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Rsan 固定口径回测")
    ap.add_argument("--data", required=True, help="后复权日线 CSV 目录，文件名含 6 位代码")
    ap.add_argument("--strategy", required=True, nargs="+", help="策略 .py 文件，可多个")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--capital", type=float, required=True, help="本金（元），必须写明；标准口径 1000000，实盘测试用自己的真实资金")
    ap.add_argument("--extra", help="额外数据目录（财报等），以 {文件名: DataFrame} 传入 ctx.extra")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    prices = load_dir(Path(a.data))
    missing = [c for c in CPLAN if c not in prices]
    if missing:
        raise SystemExit(f"缺少基准数据：{', '.join(missing)}（红利与红利稳健配置基准必须提供）")
    extra = {}
    if a.extra:
        for p in Path(a.extra).glob("*.csv"):
            extra[p.stem] = pd.read_csv(p)

    strats = [load_strategy(Path(p)) for p in a.strategy]
    # 统一窗口：所有基准都有数据的日期，再与用户指定区间、策略声明的必需标的取交集
    required = set(CPLAN)
    for m in strats:
        required |= {norm_code(str(c)) for c in getattr(m, "REQUIRED", [])}
    lack = [c for c in required if c not in prices]
    if lack:
        raise SystemExit(f"策略声明需要但数据缺失：{', '.join(sorted(lack))}")
    start = max(prices[c].index[0] for c in required)
    end = min(prices[c].index[-1] for c in required)
    warmup_days = max([getattr(m, "WARMUP_DAYS", 0) for m in strats] + [0])
    cal_all = prices[DIVIDEND_CODE].index
    if warmup_days:
        i0 = cal_all.searchsorted(start) + warmup_days
        if i0 < len(cal_all):
            start = cal_all[i0]
    if a.start:
        start = max(start, pd.Timestamp(a.start))
    if a.end:
        end = min(end, pd.Timestamp(a.end))
    cal = cal_all[(cal_all >= start) & (cal_all <= end)]
    if len(cal) < 60:
        raise SystemExit("统一窗口不足 60 个交易日")

    results = [
        run("红利510880持有", hold_dividend, prices, cal, a.capital, extra),
        run("红利稳健配置(季度再平衡)", cplan, prices, cal, a.capital, extra, rebalance="quarterly"),
    ]
    for p, m in zip(a.strategy, strats):
        name = getattr(m, "NAME", Path(p).stem)
        results.append(run(name, m.target_weights, prices, cal, a.capital, extra,
                           rebalance=getattr(m, "REBALANCE", "daily"),
                           min_trade_frac=getattr(m, "MIN_TRADE_FRAC", 0.0)))

    out = Path(a.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"输出目录 {out} 已存在且不为空：每次回测必须用新目录，避免新旧结果混在一起")
    out.mkdir(parents=True, exist_ok=True)
    repro = archive_inputs(out, a, Path(__file__).resolve())
    navdf = pd.concat([r.nav / r.nav.iloc[0] for r in results], axis=1)
    navdf.to_csv(out / "nav.csv", encoding="utf-8-sig")
    for r in results[2:]:
        if len(r.trades):
            r.trades.to_csv(out / f"trades_{r.name}.csv", index=False, encoding="utf-8-sig")

    M = {r.name: metrics(r.nav) for r in results}
    Y = pd.concat({r.name: yearly(r.nav) for r in results}, axis=1)
    half = cal[len(cal) // 2]
    H = {}
    for r in results:
        H[r.name] = (metrics(r.nav.loc[:half])["年化"], metrics(r.nav.loc[half:])["年化"])
    div, cp = results[0].name, results[1].name
    years = (cal[-1] - cal[0]).days / 365.25

    verdicts = {}
    for r in results[2:]:
        s = M[r.name]["年化"]
        if s <= M[div]["年化"]:
            v = "❌ 淘汰：年化没跑赢红利"
        elif s <= M[cp]["年化"]:
            v = "⚠️ 跑赢红利，没跑赢红利稳健配置"
        else:
            v = "✅ 同时跑赢红利和红利稳健配置"
        h1 = H[r.name][0] > H[div][0]
        h2 = H[r.name][1] > H[div][1]
        if not (h1 and h2) and not v.startswith("❌"):
            v += "；但前后两半窗口里只有一半跑赢红利，稳定性存疑"
        verdicts[r.name] = v

    lines = [f"# 回测报告", "",
             f"引擎版本 {ENGINE_VERSION}｜统一窗口 **{cal[0].date()} → {cal[-1].date()}**（{len(cal)} 个交易日，{years:.1f} 年）｜初始资金 {a.capital:,.0f}", ""]
    if verdicts:
        lines += ["## 结论", ""] + [f"- **{k}**：{v}" for k, v in verdicts.items()] + [""]
    lines += ["## 核心指标", "", "| 方案 | 年化 | 总收益 | 最大回撤 | 夏普 | 卡玛 | 年化波动 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        m = M[r.name]
        lines.append(f"| {r.name} | {pct(m['年化'])} | {pct(m['总收益'])} | {pct(m['最大回撤'])} | "
                     f"{m['夏普(rf=2%)']:.2f} | {m['卡玛']:.2f} | {pct(m['年化波动'])} |")
    lines += ["", "## 稳定性：前后两半窗口年化", "", f"分界日 {half.date()}", "", "| 方案 | 前半 | 后半 |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r.name} | {pct(H[r.name][0])} | {pct(H[r.name][1])} |")
    lines += ["", "## 最大回撤区间", ""] + [f"- {r.name}：{M[r.name]['回撤区间']}" for r in results]
    lines += ["", "## 分年度收益", "", "| 年份 | " + " | ".join(Y.columns) + " |", "|---" * (len(Y.columns) + 1) + "|"]
    for yv, row in Y.iterrows():
        lines.append(f"| {yv} | " + " | ".join(pct(x) for x in row) + " |")
    lines += ["", "## 交易与成本", "", "| 方案 | 成交笔数 | 累计成交额/初始资金 | 累计费用 | 费用拖累(年化) | 平均现金占比 |", "|---|---|---|---|---|---|"]
    cash_notes = []
    for r in results:
        drag = r.fees / a.capital / years
        cr = float(r.cash_ratio.mean())
        lines.append(f"| {r.name} | {len(r.trades)} | {r.turnover / a.capital:.1f} 倍 | {r.fees:,.0f} | {pct(drag)} | {pct(cr)} |")
        if cr > CASH_WARN:
            cash_notes.append(f"⚠️ {r.name} 平均现金占比 {cr:.1%}，超过 {CASH_WARN:.0%}。两种可能：①本金太小，买不起整手（如十年国债ETF一手约1.3万元），这是真实限制；"
                              f"②策略写法有错，钱没投出去。判断方法：用 --capital 1000000 再跑一次，100万下现金占比仍超过 {CASH_WARN:.0%} 就是写法错，结论不能用")
    notes = cash_notes + [n for r in results for n in r.notes] + data_warnings({c: prices[c] for c in prices})
    lines += ["", "## 口径", "",
              f"引擎 v{ENGINE_VERSION}：后复权、分红再投资；收盘出信号、次日开盘成交；佣金万一免五最低0.1元；股票卖出印花税0.05%、沪市过户费0.001%，ETF免；"
              f"一手100股，按真实股价取整；本金 {a.capital:,.0f} 元；现金不计息；所有方案同一窗口同一交易日历。",
              "", "## 复现", "",
              f"- 数据清单哈希：`{repro['data_hash']}`（{repro['n_files']} 个文件，逐文件清单见 repro/data_manifest.csv）",
              f"- 引擎哈希：`{repro['engine_hash']}`；策略文件夹已复制到 repro/strategies/",
              "- 原样复现：在本目录运行 `bash repro/rerun.sh`（数据目录不变时，结果应与本报告完全一致）"]
    if notes:
        lines += ["", "## 提醒", ""] + [f"- {n}" for n in dict.fromkeys(notes)]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")

    try:
        import warnings
        warnings.filterwarnings("ignore")  # 缺中文字体时的大量告警不影响结果
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        for f in ("Noto Sans CJK SC", "Noto Serif CJK SC", "WenQuanYi Zen Hei", "SimHei", "PingFang SC", "Microsoft YaHei"):
            if any(f == x.name for x in font_manager.fontManager.ttflist):
                plt.rcParams["font.sans-serif"] = [f]
                break
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
        for c in navdf.columns:
            ax[0].plot(navdf.index, navdf[c], label=c, lw=1.3)
            ax[1].plot(navdf.index, navdf[c] / navdf[c].cummax() - 1, lw=1)
        ax[0].set_title("净值（统一窗口）")
        ax[0].legend()
        ax[0].grid(alpha=.3)
        ax[1].set_title("回撤")
        ax[1].grid(alpha=.3)
        fig.tight_layout()
        fig.savefig(out / "nav.png", dpi=130)
    except Exception as e:  # 图失败不影响报告
        print("画图失败：", e, file=sys.stderr)

    summary = {"engine": ENGINE_VERSION, "capital": a.capital, "repro": repro,
               "window": [str(cal[0].date()), str(cal[-1].date())],
               "metrics": {k: {kk: (vv if isinstance(vv, str) else round(float(vv), 6)) for kk, vv in v.items()} for k, v in M.items()},
               "verdicts": verdicts}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print((out / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
