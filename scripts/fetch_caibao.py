#!/usr/bin/env python3
"""抓命脉股票池的财报数据（带实际公告日），给"财报惊喜"等需要按公告日对齐的回测用。

在 GitHub 服务器上运行（jobs/run.txt 写 fetch_caibao.py 推送）。产出：
  data/财报/单季/<代码>.csv  报告期,扣非净利润,营业总收入,净利润,公告日,公告日来源
  data/财报/年度/<代码>.csv  报告期,净利润,每股经营现金流,净资产收益率,基本每股收益
  logs/财报_最新.md          抓取情况（成功/失败、公告日覆盖率、两个来源的核对）
来源：
  - 金额：同花顺财务摘要（akshare stock_financial_abstract_ths，按单季度 / 按年度），与业绩加速选股相同
  - 公告日：baostock query_profit_data 的 pubDate（2007 年起）；巨潮"预约披露"的实际披露日作为核对
可以重复运行：已经抓好的股票会跳过（加 --force 全部重抓）。
"""
import os, sys, re, json, time, math, subprocess, datetime as dt

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUTQ = os.path.join(ROOT, "data", "财报", "单季"); OUTY = os.path.join(ROOT, "data", "财报", "年度")
os.makedirs(OUTQ, exist_ok=True); os.makedirs(OUTY, exist_ok=True)
LOGF = os.path.join(ROOT, "logs", "财报_最新.md")
FORCE = "--force" in sys.argv
T0 = time.time(); BUDGET = 80 * 60
LOG = []
def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); LOG.append(s)

def pip(*pk):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pk], check=False)
try:
    import akshare as ak
except ImportError:
    pip("akshare"); import akshare as ak
try:
    import baostock as bs
except ImportError:
    pip("baostock"); import baostock as bs
import pandas as pd

def num(x):
    if x is None: return math.nan
    s = str(x).strip().replace(",", "")
    if s in ("", "False", "nan", "None", "--"): return math.nan
    m = 1.0
    for suf, mul in (("%", .01), ("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if s.endswith(suf): s = s[:-len(suf)]; m = mul; break
    try: return float(s) * m
    except: return math.nan

def retry(fn, tries=4, wait=3):
    for i in range(tries):
        try: return fn()
        except Exception as e:
            err = e; time.sleep(wait * (i + 1))
    raise err

codes = []
for line in open(os.path.join(ROOT, "data", "标的清单.csv"), encoding="utf-8").read().splitlines()[1:]:
    p = line.split(",")
    if len(p) >= 4 and p[3] == "股票池": codes.append(p[0])
log(f"# 财报抓取 {(dt.datetime.utcnow() + dt.timedelta(hours=8)):%Y-%m-%d %H:%M}（北京时间）\n")
log(f"- 股票池 {len(codes)} 只")

# ---------- baostock 登录 ----------
bs_ok = False
try:
    lg = bs.login(); bs_ok = lg.error_code == "0"
    log(f"- baostock 登录：{'成功' if bs_ok else '失败 ' + lg.error_msg}")
except Exception as e:
    log(f"- baostock 登录失败：{type(e).__name__}: {e}")

def bs_pubdates(c):
    """返回 {报告期: 公告日}；报告期为 YYYY-MM-DD。"""
    out = {}
    if not bs_ok: return out
    sym = ("sh." if c.startswith("6") else "sz.") + c
    this_year = dt.date.today().year
    for y in range(2007, this_year + 1):
        for q in (1, 2, 3, 4):
            try:
                rs = bs.query_profit_data(code=sym, year=y, quarter=q)
                while rs.error_code == "0" and rs.next():
                    r = dict(zip(rs.fields, rs.get_row_data()))
                    if r.get("statDate") and r.get("pubDate"):
                        out[r["statDate"]] = r["pubDate"]
            except Exception:
                pass
    return out

# ---------- 巨潮实际披露日（核对用；只取得到的期） ----------
cninfo = {}
def cninfo_period(sec):
    import requests
    r = requests.post("http://www.cninfo.com.cn/new/information/getPrbookInfo", timeout=30,
                      params={"sectionTime": sec, "firstTime": "", "lastTime": "", "market": "szsh", "stockCode": "",
                              "orderClos": "", "isDesc": "", "pagesize": "10000", "pagenum": "1"},
                      headers={"User-Agent": "Mozilla/5.0"})
    return r.json().get("prbookinfos") or []
got_periods = []
for y in range(2010, dt.date.today().year + 1):
    for md in ("03-31", "06-30", "09-30", "12-31"):
        sec = f"{y}-{md}"
        if dt.date.fromisoformat(sec) > dt.date.today(): continue
        try:
            rows = retry(lambda: cninfo_period(sec), tries=2)
        except Exception as e:
            rows = []
        n = 0
        if rows and not got_periods and not cninfo:
            log(f"- 巨潮样例 {sec}：{json.dumps(rows[0], ensure_ascii=False)[:300]}")
        for r in rows:
            v = list(r.values())          # 字段顺序同 akshare：代码、简称、首次预约、实际披露……
            if len(v) < 4: continue
            c, d = str(v[0]), str(v[3] or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", d): continue
            if c and d:
                cninfo.setdefault(c, {})[sec] = str(d)[:10]; n += 1
        if n: got_periods.append(sec)
log(f"- 巨潮实际披露日：取到 {len(got_periods)} 期" + (f"（{got_periods[0]} 至 {got_periods[-1]}）" if got_periods else ""))

# ---------- 逐只抓 ----------
done, skipped, fails = 0, 0, []
check_same = check_diff = 0; diffs = []
cover = []
for i, c in enumerate(codes):
    fq, fy = os.path.join(OUTQ, c + ".csv"), os.path.join(OUTY, c + ".csv")
    if not FORCE and os.path.exists(fq) and os.path.exists(fy):
        skipped += 1; continue
    if time.time() - T0 > BUDGET:
        log(f"- 时间预算用完，停在第 {i+1} 只；再推送一次会接着抓"); break
    try:
        q = retry(lambda: ak.stock_financial_abstract_ths(symbol=c, indicator="按单季度"))
        a = retry(lambda: ak.stock_financial_abstract_ths(symbol=c, indicator="按年度"))
        pub = bs_pubdates(c)
        rows = []
        for r in q.astype(str).to_dict("records"):
            p = str(r.get("报告期"))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p): continue
            d, src = pub.get(p, ""), "baostock"
            cn = cninfo.get(c, {}).get(p, "")
            if not d and cn: d, src = cn, "巨潮"
            if not d: src = ""
            if pub.get(p) and cn:
                if pub[p] == cn: check_same += 1
                else:
                    check_diff += 1
                    if len(diffs) < 15: diffs.append(f"{c} {p} baostock {pub[p]} / 巨潮 {cn}")
            rows.append(dict(报告期=p, 扣非净利润=num(r.get("扣非净利润")), 营业总收入=num(r.get("营业总收入")),
                             净利润=num(r.get("净利润")), 公告日=d, 公告日来源=src))
        dq = pd.DataFrame(rows).sort_values("报告期")
        dq.to_csv(fq, index=False, encoding="utf-8")
        yrows = []
        for r in a.astype(str).to_dict("records"):
            p = str(r.get("报告期"))
            if not re.fullmatch(r"\d{4}", p): continue
            roe = num(r.get("净资产收益率"))
            yrows.append(dict(报告期=p, 净利润=num(r.get("净利润")), 每股经营现金流=num(r.get("每股经营现金流")),
                              净资产收益率=roe if roe == roe else num(r.get("净资产收益率-摊薄")),
                              基本每股收益=num(r.get("基本每股收益"))))
        pd.DataFrame(yrows).sort_values("报告期").to_csv(fy, index=False, encoding="utf-8")
        recent = dq[dq["报告期"] >= "2010-01-01"]
        cover.append((c, len(recent), int((recent["公告日"] != "").sum())))
        done += 1
        if done % 10 == 0: log(f"  已抓 {done} 只，用时 {(time.time()-T0)/60:.0f} 分钟")
    except Exception as e:
        fails.append(f"{c}：{type(e).__name__}: {str(e)[:120]}")
try: bs.logout()
except Exception: pass

log(f"- 本次抓到 {done} 只，跳过已有 {skipped} 只，失败 {len(fails)} 只；用时 {(time.time()-T0)/60:.1f} 分钟")
if cover:
    tot = sum(x[1] for x in cover); hit = sum(x[2] for x in cover)
    log(f"- 2010 年起单季报告 {tot} 期，有公告日 {hit} 期（{hit/max(tot,1):.1%}）")
    low = [f"{c}（{h}/{t}）" for c, t, h in cover if t and h / t < 0.8]
    if low: log(f"- 公告日覆盖不足八成：{'、'.join(low[:30])}")
log(f"- 公告日核对（两个来源都有的期）：一致 {check_same}，不一致 {check_diff}")
for d in diffs: log(f"  - {d}")
for f in fails: log(f"- 失败 {f}")
open(LOGF, "w", encoding="utf-8").write("\n".join(LOG) + "\n")
