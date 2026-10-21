#!/usr/bin/env python3
"""业绩加速（v3）季报选股 —— GitHub 服务器版。

由 Mac 上 ~/股票助理/v3_select.py（2026-10-11 版）迁移而来，选股规则一字不改，只换了取数：
  - 东财的实时行情、历史成交额在 GitHub 服务器上连不上，改用腾讯：
      实时价、总市值 ← qt.gtimg.cn；上市年限、日均成交额 ← 腾讯日K（成交额 = 成交量×收盘价，近似）
  - 申万行业成分、同花顺年报/单季财报、新浪分红 仍用 akshare，与原版相同。

规则：
  1) 范围：14 个命脉相关申万一级行业的成分股，按最新年报跑第一层规则；
  2) 加速：最新季报单季扣非同比 20%–200%、比上季更快、上季同比 >10%、营收同比增长；
  3) 排序：按单季增速从高到低，同一行业最多 1 只，取前 N 只；
  4) 对照上期名单，给出卖出 / 买入 / 继续持有，以及按资金算好的股数（整手）。

结果写到 data/v3/<换股日>.json、data/v3/latest.json、data/v3/最新名单.md，日志写 logs/v3_最新.md。
用法：
  python3 scripts/v3_select.py                     # 只有在换股窗口内且本期还没算过时才运行
  python3 scripts/v3_select.py --force             # 立即重算本期
  python3 scripts/v3_select.py --test --asof=2026-09-01   # 测试：按指定日期算，结果写到 logs/v3_test/，不影响正式名单
"""
import os, sys, json, time, math, re, warnings, subprocess, urllib.request, urllib.error, datetime as dt
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TEST = "--test" in sys.argv
OUT = os.path.join(ROOT, "logs", "v3_test") if TEST else os.path.join(ROOT, "data", "v3")
os.makedirs(OUT, exist_ok=True)
CACHE = os.path.join(ROOT, ".cache", "v3"); os.makedirs(CACHE, exist_ok=True)   # 不提交进仓库
LOGF = os.path.join(OUT if TEST else os.path.join(ROOT, "logs"), "v3_最新.md")

CFG = {
    "capital": 20000,        # 投入资金（元）
    "n_hold": 5,             # 持股只数：2万元时5–6只，5万以上10只
    "g_min": 0.20, "g_max": 2.00, "prev_min": 0.10,
    "mcap_min": 150e8, "amt_min": 0.5e8, "list_years": 5,
}
cfg_path = os.path.join(ROOT, "data", "v3", "config.json")
if os.path.exists(cfg_path):
    CFG.update(json.load(open(cfg_path, encoding="utf-8")))
else:
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    json.dump(CFG, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

SW = {"801160": "公用事业", "801960": "石油石化", "801770": "通信", "801750": "计算机", "801080": "电子",
      "801010": "农林牧渔", "801170": "交通运输", "801150": "医药生物", "801780": "银行", "801790": "非银金融",
      "801950": "煤炭", "801120": "食品饮料", "801110": "家用电器", "801050": "有色金属"}
FIN = {"银行", "非银金融"}

def today_bj():
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()

def period_of(d):
    """返回当前所属的换股日（5/1、9/1、11/1 中最近的一个）。"""
    for m in (11, 9, 5):
        p = dt.date(d.year, m, 1)
        if d >= p: return p
    return dt.date(d.year - 1, 11, 1)

LOG = []
def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); LOG.append(s)

# ---------- 数值解析 ----------
def num(x):
    if x is None: return math.nan
    s = str(x).strip().replace(",", "")
    if s in ("", "False", "nan", "None", "--"): return math.nan
    m = 1.0
    for suf, mul in (("%", .01), ("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if s.endswith(suf): s = s[:-len(suf)]; m = mul; break
    try: return float(s) * m
    except: return math.nan

# ---------- 取数（带缓存与重试） ----------
FAILS = []
def cached(name, fn, max_age_days=3):
    p = os.path.join(CACHE, name + ".json")
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age_days * 86400:
        return json.load(open(p, encoding="utf-8"))
    err = None
    for k in range(3):
        try:
            v = fn(); json.dump(v, open(p, "w", encoding="utf-8"), ensure_ascii=False); time.sleep(0.5); return v
        except Exception as e:
            err = e; time.sleep(2 + 3 * k)
    log(f"  取数失败 {name}: {str(err)[:80]}"); FAILS.append(name)
    return None

import threading
_lock = threading.Lock(); _next = [0.0]
PACE = 0.8  # 腾讯两次请求至少间隔的秒数（太密会返回 501，与 update_hangqing.py 相同）

def _get(url, ref="https://gu.qq.com", tries=5):
    """腾讯接口：全局节流；被限流时所有线程一起暂停。"""
    last = None
    for i in range(tries):
        with _lock:
            wait = _next[0] - time.time()
            if wait > 0: time.sleep(wait)
            _next[0] = time.time() + PACE
        try:
            r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": ref})
            return urllib.request.urlopen(r, timeout=30).read().decode("gbk", "ignore")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429, 501, 503):
                with _lock: _next[0] = time.time() + 30 * (i + 1)
            else: time.sleep(2 * (i + 1))
        except Exception as e:
            last = e; time.sleep(2 * (i + 1))
    raise RuntimeError(f"请求失败 {last!r}")

def _sym(c):
    return ("sh" if c.startswith(("6", "9")) else "sz") + c

class Live:
    def __init__(self):
        try:
            import akshare as ak
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "akshare"], check=False)
            import akshare as ak
        self.ak = ak
    def constituents(self):
        """申万成分：取到就存档到 data/v3/申万成分.json；某个行业取不到时用存档（成分股半年才调整一次）。"""
        arch_p = os.path.join(ROOT, "data", "v3", "申万成分.json")
        arch = json.load(open(arch_p, encoding="utf-8")) if os.path.exists(arch_p) else {}
        out, used_arch = {}, []
        for code, name in SW.items():
            rows = cached(f"cons_{code}", lambda c=code: self.ak.index_component_sw(symbol=c).astype(str).to_dict("records"), 30)
            if rows:
                arch[code] = {"date": today_bj().isoformat(), "rows": rows}
            elif code in arch:
                rows = arch[code]["rows"]; used_arch.append(f"{name}（{arch[code]['date']}）")
            else:
                rows = []
            for r in rows:
                k = [v for kk, v in r.items() if "代码" in kk][0]
                n = [v for kk, v in r.items() if "简称" in kk or "名称" in kk]
                out[str(k).zfill(6)] = {"sw": name, "name": n[0] if n else ""}
        if used_arch: log(f"⚠️ 申万成分取不到，改用存档：{'、'.join(used_arch)}")
        json.dump(arch, open(arch_p, "w", encoding="utf-8"), ensure_ascii=False)
        return out
    def spot(self, codes):
        """腾讯实时行情：价格（元）、总市值（字段45，单位亿元）。"""
        out, codes = {}, sorted(codes)
        for i in range(0, len(codes), 60):
            batch = codes[i:i + 60]
            def f(b=batch):
                t = _get("https://qt.gtimg.cn/q=" + ",".join(_sym(c) for c in b))
                res = {}
                for line in t.strip().split(";"):
                    if "~" not in line: continue
                    x = line.split("~")
                    if len(x) > 45: res[x[2]] = {"name": x[1], "price": num(x[3]), "mcap": num(x[45]) * 1e8}
                return res
            out.update(cached(f"spot_{i}_{batch[0]}", f, 1) or {})
        return out
    def hist(self, c, asof):
        """腾讯周K（不复权），一次请求取换股日前约5.6年，返回 dict：
        first=第一根周线日期（判断上市年限）；days=近一年交易日数；amt=近一年日均成交额（周成交量×100×周收盘价，近似）。"""
        def f():
            a = asof - dt.timedelta(days=int(365 * (CFG["list_years"] + 0.6)))
            t = _get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={_sym(c)},week,{a},{asof - dt.timedelta(days=1)},320,")
            d = (json.loads(t).get("data") or {}).get(_sym(c)) or {}
            rows = [r for r in (d.get("week") or d.get("qfqweek") or []) if len(r) >= 6]
            if not rows: return None
            y1 = (asof - dt.timedelta(days=365)).isoformat()
            last = [r for r in rows if r[0] > y1]
            amt = sum(float(r[5]) * 100 * float(r[2]) for r in last)
            days = round(len(last) * 4.85)          # A股一年约 242 个交易日 / 50 周
            return dict(first=rows[0][0], days=days, amt=amt / days if days else 0, weeks=len(last))
        return cached(f"histw_{c}_{asof}", f, 3)
    def annual(self, c):
        return cached(f"fy_{c}", lambda: self.ak.stock_financial_abstract_ths(symbol=c, indicator="按年度").astype(str).to_dict("records"), 20) or []
    def quarterly(self, c):
        return cached(f"fq_{c}", lambda: self.ak.stock_financial_abstract_ths(symbol=c, indicator="按单季度").astype(str).to_dict("records"), 5) or []
    def dividends(self, c):
        return cached(f"div_{c}", lambda: self.ak.stock_history_dividend_detail(symbol=c, indicator="分红").astype(str).to_dict("records"), 20) or []

T0 = time.time()
BUDGET = 75 * 60          # 运行满 75 分钟主动停下，避免被 GitHub 90 分钟上限强制杀掉
STOPPED = []
def elapsed(): return time.time() - T0
def over_budget():
    if elapsed() > BUDGET:
        if not STOPPED: STOPPED.append(1); log(f"⚠️ 已运行 {elapsed()/60:.0f} 分钟，超过时间预算，停止取数，以下结果不完整")
        return True
    return False

def pmap(fn, items, workers=4):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda x: None if over_budget() else fn(x), items))

# ---------- 第一层（与原版相同） ----------
def fy_table(rows):
    t = {}
    for r in rows:
        if re.fullmatch(r"\d{4}", str(r.get("报告期"))):
            roe = num(r.get("净资产收益率"))
            t[int(r["报告期"])] = dict(np=num(r.get("净利润")), eps=num(r.get("基本每股收益")), ocfps=num(r.get("每股经营现金流")),
                                      roe=roe if roe == roe else num(r.get("净资产收益率-摊薄")))
    return t

def div_by_fy(rows):
    """财年 y 的现金分红（元/股，按除权时股本；送转后折算回财年末股本）。窗口：y年9/1 至 y+1年9/1。"""
    ev = []
    for r in rows:
        if str(r.get("进度")) != "实施": continue
        try: ex = dt.date.fromisoformat(str(r.get("除权除息日"))[:10])
        except: continue
        cash = (num(r.get("派息")) or 0) / 10; split = ((num(r.get("送股")) or 0) + (num(r.get("转增")) or 0)) / 10
        ev.append((ex, 0 if cash != cash else cash, 0 if split != split else split))
    ev.sort()
    def fy(y):
        a, b = dt.date(y, 9, 1), dt.date(y + 1, 9, 1); tot = 0.0; mult = 1.0
        for ex, cash, split in ev:
            if a <= ex < b: tot += cash * mult; mult *= (1 + split)
        return tot
    return fy

def layer1(fy, dfy, Y, isfin):
    need = [Y - 2, Y - 1, Y]
    if any(y not in fy for y in need): return None, "年报不全"
    f = [fy[y] for y in need]
    if not all(x["np"] > 0 for x in f): return None, "三年有亏损"
    if not isfin and not all(x["ocfps"] > 0 for x in f): return None, "经营现金流非正"
    np3 = fy.get(Y - 3, {}).get("np", math.nan); npY, np1 = f[2]["np"], f[1]["np"]; roe = f[2]["roe"]
    growth = np3 == np3 and npY > np3 * 1.03 and npY >= np1 * 0.9 and roe >= 0.10
    ocf_sum = sum(x["ocfps"] * x["np"] / x["eps"] for x in f if x["eps"] and x["eps"] > 0)
    cyc = np3 == np3 and npY >= np3 * 0.8 and (isfin or (roe >= 0.08 and ocf_sum >= 0.5 * sum(x["np"] for x in f)))
    income = False
    if dfy is not None:
        pays = [dfy(y) / fy[y]["eps"] if fy[y]["eps"] and fy[y]["eps"] > 0 else 0 for y in need]
        income = all(p >= 0.30 for p in pays) and dfy(Y) > 0 and dfy(Y) >= dfy(Y - 3) * 0.97
    typ = "收租" if income else ("成长" if growth else ("周期" if cyc else ""))
    return (typ or None), ("" if typ else "类型条件都不满足")

# ---------- 季报加速（与原版相同） ----------
def q_signal(rows, asof):
    deadline = {3: (4, 30), 6: (8, 31), 9: (10, 31), 12: (4, 30)}
    q = {}
    for r in rows:
        p = str(r.get("报告期"))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p): continue
        d = dt.date.fromisoformat(p)
        if d.month not in deadline: continue
        kf = num(r.get("扣非净利润")); kf = kf if kf == kf else num(r.get("净利润"))
        q[d] = dict(kf=kf, rev=num(r.get("营业总收入")))
    def avail(d):
        m, dd = deadline[d.month]; return dt.date(d.year + (1 if d.month == 12 else 0), m, dd)
    seen = sorted(d for d in q if avail(d) < asof)
    if not seen: return None
    last = seen[-1]
    if (asof - last).days > 250: return None
    def yoy(d, k):
        p = dt.date(d.year - 1, d.month, d.day if d.month != 2 else 28)
        if p not in q or not (q[p][k] > 0) or q[d][k] != q[d][k]: return math.nan
        return q[d][k] / q[p][k] - 1
    prev = seen[-2] if len(seen) >= 2 else None
    g = yoy(last, "kf"); gp = yoy(prev, "kf") if prev else math.nan
    return dict(q=last.isoformat(), g=g, gprev=gp, acc=g - gp, grev=yoy(last, "rev"))

# ---------- 主流程（规则与原版相同，取数改为并发） ----------
def select(src, asof, prev_hold):
    Y = asof.year - 1 if asof.month >= 5 else asof.year - 2           # 已出齐的最新年报
    cons = src.constituents()
    pool = [c for c in sorted(cons) if not c.startswith(("4", "8", "9"))]
    spot = src.spot(pool)
    log(f"行业成分 {len(cons)} 只（取到行情 {len(spot)} 只）；年报口径 {Y}；季报截止 {asof}")
    pre = [c for c in pool if c in spot and "ST" not in spot[c]["name"].upper() and spot[c]["mcap"] >= CFG["mcap_min"]]
    hists = {}
    for i, c in enumerate(pre):                      # 腾讯接口单线程匀速请求
        if over_budget(): break
        hists[c] = src.hist(c, asof)
        if (i + 1) % 100 == 0: log(f"  周K进度 {i+1}/{len(pre)}，已用 {elapsed()/60:.0f} 分钟")
    cands = []
    for c in pre:
        h = hists.get(c)
        if not h: continue
        if h["days"] < 200: continue          # 近一年不足约200个交易日（停牌过多或上市不满一年）
        first = dt.date.fromisoformat(h["first"][:10])
        if (asof - first).days < 365 * CFG["list_years"] - 30: continue
        if h["amt"] < CFG["amt_min"]: continue
        cands.append(c)
    log(f"总市值≥{CFG['mcap_min']/1e8:.0f}亿 {len(pre)} 只；规模、流动性、上市年限达标 {len(cands)} 只")
    annual = dict(zip(cands, pmap(src.annual, cands)))
    passed, need_div = [], []
    for c in cands:
        fy = fy_table(annual[c] or []); isfin = cons[c]["sw"] in FIN
        typ, why = layer1(fy, None, Y, isfin)
        if typ: passed.append((c, typ))
        elif why == "类型条件都不满足": need_div.append(c)
    divs = dict(zip(need_div, pmap(src.dividends, need_div)))
    for c in need_div:
        typ, why = layer1(fy_table(annual[c] or []), div_by_fy(divs[c] or []), Y, cons[c]["sw"] in FIN)
        if typ: passed.append((c, typ))
    passed.sort()
    log(f"第一层通过 {len(passed)} 只")
    quarts = dict(zip([c for c, _ in passed], pmap(src.quarterly, [c for c, _ in passed])))
    rows = []
    for c, typ in passed:
        sig = q_signal(quarts[c] or [], asof)
        if not sig: continue
        ok = (CFG["g_min"] < sig["g"] <= CFG["g_max"]) and sig["acc"] > 0 and sig["gprev"] > CFG["prev_min"] and sig["grev"] > 0
        rows.append(dict(code=c, name=spot.get(c, {}).get("name") or cons[c]["name"], sw=cons[c]["sw"], typ=typ,
                         price=spot.get(c, {}).get("price"), ok=ok, **sig))
    ok = sorted([r for r in rows if r["ok"]], key=lambda r: -r["g"])
    log(f"符合加速条件 {len(ok)} 只")
    # 排序选股：同行业最多1只；一手买不起（超过每只预算）的跳过
    per = CFG["capital"] / CFG["n_hold"]
    picks, secs, skipped = [], set(), []
    # 仍符合条件的老持仓优先保留（不重复换手）
    keep = [r for r in ok if r["code"] in prev_hold]
    for r in keep + [r for r in ok if r["code"] not in prev_hold]:
        if len(picks) >= CFG["n_hold"]: break
        if r["sw"] in secs: continue
        if r["price"] and r["price"] * 100 > per * 1.15:
            skipped.append(r); continue
        picks.append(r); secs.add(r["sw"])
    for r in picks:
        r["shares"] = int(per // (r["price"] * 100)) * 100 if r["price"] else None
        r["amount"] = round(r["shares"] * r["price"], 2) if r["shares"] else None
    new = {r["code"] for r in picks}
    return dict(asof=asof.isoformat(), fy=Y, generated=(dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
                config=CFG, source="GitHub服务器（腾讯行情+akshare财报）",
                counts=dict(industry=len(cons), mcap_ok=len(pre), size_ok=len(cands), layer1=len(passed), accel=len(ok)),
                fetch_fail=len(FAILS), incomplete=bool(STOPPED),
                picks=picks, sell=sorted(set(prev_hold) - new), buy=sorted(new - set(prev_hold)), keep=sorted(new & set(prev_hold)),
                skipped_lot=[dict(code=r["code"], name=r["name"], price=r["price"]) for r in skipped],
                bench=ok[:30])

def write(res):
    p = os.path.join(OUT, f"{res['asof']}.json")
    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(res, open(os.path.join(OUT, "latest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    name = {r["code"]: r["name"] for r in res["bench"]}
    L = [f"# v3 换股名单（{res['asof']}）" + ("　⚠️未跑完，名单不可用" if res.get("incomplete") else ""), "", f"生成：{res['generated']}（{res['source']}）　年报口径：{res['fy']}　资金：{res['config']['capital']}元　持股：{res['config']['n_hold']}只", "",
         f"范围 {res['counts']['industry']} → 规模达标 {res['counts']['size_ok']} → 第一层通过 {res['counts']['layer1']} → 加速 {res['counts']['accel']}"
         + (f"（取数失败 {res['fetch_fail']} 次，见日志）" if res["fetch_fail"] else ""), "",
         "| 代码 | 名称 | 行业 | 单季扣非增速 | 上季增速 | 营收增速 | 参考价 | 建议股数 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in res["picks"]:
        L.append(f"| {r['code']} | {r['name']} | {r['sw']} | {r['g']:.0%} | {r['gprev']:.0%} | {r['grev']:.0%} | {r['price']} | {r['shares']} |")
    L += ["", f"卖出：{'、'.join(res['sell']) or '无'}", f"买入：{'、'.join(name.get(c, c) for c in res['buy']) or '无'}",
          f"继续持有：{'、'.join(name.get(c, c) for c in res['keep']) or '无'}", "",
          "规则：下一个交易日按名单调整；两次换股之间不止损、不止盈、不加仓。以上按规则生成，仅供参考，不构成投资建议。"]
    open(os.path.join(OUT, "最新名单.md"), "w", encoding="utf-8").write("\n".join(L))
    log("\n".join(L))

def main():
    force = "--force" in sys.argv or TEST
    a = [x.split("=", 1)[1] for x in sys.argv if x.startswith("--asof=")]
    d = dt.date.fromisoformat(a[0]) if a else today_bj(); per = period_of(d)
    done = os.path.exists(os.path.join(OUT, f"{per.isoformat()}.json"))
    if not force and (done or (d - per).days > 20):
        print(f"v3：本期（{per}）{'已算过' if done else '不在换股窗口'}，跳过。"); return
    prev = {}
    olds = sorted(f for f in os.listdir(OUT) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f) and f < f"{per.isoformat()}.json")
    if olds: prev = {r["code"] for r in json.load(open(os.path.join(OUT, olds[-1]), encoding="utf-8"))["picks"]}
    t = T0
    try:
        res = select(Live(), per, prev)
        bad = []
        if res["counts"]["industry"] < 1500: bad.append(f"行业成分只有 {res['counts']['industry']} 只（正常约 2300 只）")
        if res["counts"]["size_ok"] == 0: bad.append("规模达标 0 只")
        if res.get("incomplete"): bad.append("超过时间预算没跑完")
        if bad:
            log("❌ 本次结果不可用，未写入正式名单：" + "；".join(bad))
            json.dump(res, open(os.path.join(OUT, f"失败_{per.isoformat()}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            FAILED.append(1)
        else:
            write(res)
    finally:
        log(f"\n用时 {time.time()-t:.0f} 秒；取数失败 {len(FAILS)} 次" + (f"：{'、'.join(FAILS[:30])}" if FAILS else ""))
        open(LOGF, "w", encoding="utf-8").write(f"# v3 选股运行日志（{(dt.datetime.utcnow() + dt.timedelta(hours=8)):%Y-%m-%d %H:%M}，换股日 {per}）\n\n```\n" + "\n".join(LOG) + "\n```\n")

FAILED = []
if __name__ == "__main__":
    main()
    if FAILED: sys.exit(1)
