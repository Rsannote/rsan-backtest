#!/usr/bin/env python3
"""市场状态模块的准备探测（只探测，不入库）：
1) 核对候选行业 ETF 代码的名称（腾讯实时行情）；
2) 看 qlib 开源数据集（chenditc/investment_data）里全市场成交额的单位和最新日期，便于回补历史；
3) 东财两融历史能翻多少页。
结果写 logs/explore/state/。用法：python3 scripts/explore_state.py
"""
import json
import os
import struct
import subprocess
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "explore", "state")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}

CAND = ["512480", "515880", "512720", "159819", "512980", "159869", "562500", "515030", "159755", "515790",
        "512660", "512880", "512800", "512070", "512010", "159992", "512170", "512690", "515170", "159996",
        "516110", "159865", "159825", "515220", "512400", "515210", "159870", "512200", "516950", "159611",
        "159930", "159766", "159666", "561320", "516910", "159301", "560880", "159619", "516160", "159995",
        "512690", "515050", "159928", "512580", "516070", "159698", "515000", "159807"]


def get(url, enc="utf-8"):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode(enc, errors="replace")


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {}
    # 1 名称
    syms = [("sh" if c.startswith(("5", "6")) else "sz") + c for c in dict.fromkeys(CAND)]
    txt = get("https://qt.gtimg.cn/q=" + ",".join(syms), "gbk")
    names = {}
    for line in txt.split(";"):
        if "~" in line:
            f = line.split('"')[1].split("~")
            names[f[2]] = {"name": f[1], "price": f[3], "amount_wan": f[37] if len(f) > 37 else ""}
    res["etf_names"] = names
    # 首日：腾讯日K取 2000 条看最早日期
    first = {}
    for s in syms:
        try:
            js = json.loads(get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={s},day,2005-01-01,,8000,"))
            day = js["data"][s].get("day") or js["data"][s].get("qfqday") or []
            first[s[2:]] = day[0][0] if day else ""
        except Exception as e:
            first[s[2:]] = f"err {e}"[:60]
    res["etf_first_day"] = first

    # 2 qlib 成交额
    try:
        tgz = os.path.join(OUT, "qlib.tar.gz")
        subprocess.run(["curl", "-sSL", "-m", "900", "-o", tgz,
                        "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"], check=True)
        d = os.path.join(OUT, "q")
        os.makedirs(d, exist_ok=True)
        subprocess.run(["tar", "-xzf", tgz, "-C", d, "--wildcards", "qlib_bin/calendars/*", "qlib_bin/instruments/*",
                        "*/amount.day.bin", "*/close.day.bin"], check=True)
        cal = open(os.path.join(d, "qlib_bin/calendars/day.txt")).read().split()
        fdir = os.path.join(d, "qlib_bin/features")
        n = len(cal)
        tot = [0.0] * n
        cnt = 0
        for inst in os.listdir(fdir):
            p = os.path.join(fdir, inst, "amount.day.bin")
            if not os.path.exists(p) or not (inst.startswith("sh6") or inst.startswith("sz0") or inst.startswith("sz3")):
                continue
            b = open(p, "rb").read()
            arr = struct.unpack(f"<{len(b)//4}f", b)
            start = int(arr[0])
            for i, v in enumerate(arr[1:]):
                if v == v and start + i < n:
                    tot[start + i] += v
            cnt += 1
        res["qlib"] = {"last": cal[-1], "first": cal[0], "n_stocks": cnt,
                       "recent_total_amount_raw": {cal[i]: tot[i] for i in range(n - 8, n)}}
        os.remove(tgz)
        import shutil
        shutil.rmtree(d)
    except Exception as e:
        import shutil
        shutil.rmtree(os.path.join(OUT, "q"), ignore_errors=True)
        if os.path.exists(os.path.join(OUT, "qlib.tar.gz")):
            os.remove(os.path.join(OUT, "qlib.tar.gz"))
        res["qlib"] = f"err {type(e).__name__}: {e}"[:300]

    # 3 两融历史
    try:
        js = json.loads(get("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ&columns=DIM_DATE,RZYE,RZRQYE&source=WEB&sortColumns=DIM_DATE&sortTypes=-1&pageNumber=1&pageSize=500"))
        dd = js["result"]["data"]
        res["margin"] = {"pages_at_500": js["result"]["pages"], "newest": dd[0], "oldest_in_page": dd[-1]}
    except Exception as e:
        res["margin"] = f"err {e}"[:200]

    with open(os.path.join(OUT, "结果.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
