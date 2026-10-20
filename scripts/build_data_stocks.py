#!/usr/bin/env python3
"""生成 v1.1 引擎用的标准日线目录：每个标的一个 CSV，列 date/open/close/factor（后复权价、factor=后复权价/真实价）。

个股：开源数据集 chenditc/investment_data 的 qlib_bin（GitHub Releases 下载 qlib_bin.tar.gz，约540MB）。
      open/close 为真实价，adjclose 为后复权收盘；factor = adjclose/close。
ETF ：股票助理 data/hongli/sina_<代码>_raw.csv（新浪不复权）+ sina_<代码>_hfq.txt（分红/拆分表），
      或 data/rebound/<代码>_sina.csv + <代码>_div.csv（累计分红表）；按除息日 F×=前收/(前收−每份分红)、拆分日 F×=拆分比例。
      每次分红都写进 <输出目录>_分红核对.csv，和当天开盘跳空对照。

用法：
  python3 build_data.py --out ../回测数据/日线_YYYY-MM-DD --qlib <解压后的qlib_bin目录> --stocks 600900,300308 --etfs 510880,513100,511260,518880,510300
  --stocks-from-v3 ../out/v3_hold_for_cloud.json   # 取业绩加速历期名单里出现过的全部股票（2018-05起）
输出目录必须是新的（不覆盖旧数据，旧回测才能复现）。
"""
import argparse, json, os, sys
import numpy as np, pandas as pd

H = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 股票助理


def stocks(Q, codes, out, start="2018-01-01"):
    cal = pd.to_datetime([l.strip() for l in open(f"{Q}/calendars/day.txt")])
    def rd(p, f):
        a = np.fromfile(f"{Q}/features/{p}/{f}.day.bin", dtype="<f4"); s = int(a[0]); v = a[1:]
        return pd.Series(v.astype(float), index=cal[s:s + len(v)])
    miss = []
    for c in sorted(codes):
        p = ("sh" if c[0] in "69" else "sz") + c
        try:
            o, cl, adj = rd(p, "open"), rd(p, "close"), rd(p, "adjclose")
        except FileNotFoundError:
            miss.append(c); continue
        k = adj / cl
        df = pd.DataFrame({"open": o * k, "close": adj, "factor": k}).dropna()
        df = df[df.index >= start]; df.index.name = "date"
        df.round({"open": 4, "close": 4, "factor": 6}).to_csv(f"{out}/{c}.csv")
    if miss:
        print("⚠️ 数据集里找不到：", miss)


def etf(c, out, log):
    a, b = f"{H}/data/hongli/sina_{c}_raw.csv", f"{H}/data/hongli/sina_{c}_hfq.txt"
    if os.path.exists(a) and os.path.exists(b):
        r = pd.read_csv(a, parse_dates=["date"]).set_index("date")[["open", "close"]].astype(float)
        t = open(b).read(); j = json.loads(t[t.index("{"):t.rindex("}") + 1])["data"]
        ev, prevu = {}, 0.0
        for x in sorted(j, key=lambda x: x["d"]):
            if x["d"] <= "1901": continue
            u = float(x["u"]); ev[pd.Timestamp(x["d"])] = (float(x["s"]), u - prevu); prevu = u
    else:
        r = pd.read_csv(f"{H}/data/rebound/{c}_sina.csv", parse_dates=["date"]).set_index("date")[["open", "close"]].astype(float)
        dv = pd.read_csv(f"{H}/data/rebound/{c}_div.csv"); ev, prevu = {}, 0.0
        for dt, u in zip(pd.to_datetime(dv.iloc[:, 0]), dv.iloc[:, 1]):
            ev[dt] = (1.0, float(u) - prevu); prevu = float(u)
    F, f = [], 1.0
    for i, dt in enumerate(r.index):
        if dt in ev and i > 0:
            s, div = ev[dt]; pc = r["close"].iloc[i - 1]
            if s != 1.0: f *= s
            if div > 0:
                f *= pc / (pc - div)
                log.append((c, str(dt.date()), round(div, 4), round(r["open"].iloc[i] / pc, 4), round((pc - div) / pc, 4)))
        F.append(f)
    for dt in ev:
        if dt not in r.index: print(f"⚠️ {c} 除息日 {dt.date()} 不是交易日，请人工核对")
    F = pd.Series(F, index=r.index)
    o = pd.DataFrame({"open": r["open"] * F, "close": r["close"] * F, "factor": F}); o.index.name = "date"
    o.round({"open": 5, "close": 5, "factor": 6}).to_csv(f"{out}/{c}.csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--qlib")
    ap.add_argument("--stocks", default=""); ap.add_argument("--stocks-from-v3")
    ap.add_argument("--etfs", default="510880,513100,511260,518880,510300")
    a = ap.parse_args()
    if os.path.exists(a.out) and os.listdir(a.out):
        sys.exit(f"{a.out} 已存在且不为空：数据目录不覆盖，请换一个新目录名")
    os.makedirs(a.out, exist_ok=True)
    codes = {c for c in a.stocks.split(",") if c}
    if a.stocks_from_v3:
        d = json.load(open(a.stocks_from_v3))
        for k in ("5", "10"):
            for dt, v in d[k].items():
                if dt >= "2018-05-01": codes |= set(v)
    if codes:
        if not a.qlib: sys.exit("有个股时必须给 --qlib")
        stocks(a.qlib, codes, a.out)
    log = []
    for c in [x for x in a.etfs.split(",") if x]:
        etf(c, a.out, log)
    L = pd.DataFrame(log, columns=["code", "date", "div", "open/prevclose", "(pc-div)/pc"])
    L.to_csv(a.out.rstrip("/") + "_分红核对.csv", index=False)
    bad = L[(L["open/prevclose"] - L["(pc-div)/pc"]).abs() > 0.03]
    print(f"个股 {len(codes)} 只，ETF 分红 {len(L)} 次；开盘跳空和分红差超过3%的：{len(bad)} 次")
    if len(bad): print(bad.to_string())
