#!/usr/bin/env python3
"""T3 验收（在 Actions 上跑）：用 update_hangqing.py 把六个海外指数建库，再逐项核对。

- 起点：五个指数 2010-01-04 前后起，恒生科技 2020-07-27 起；
- 抽 5 天收盘与第二个公开来源对照：纳指/标普对新浪美股，恒指对腾讯；日经/KOSPI/恒生科技没有第二个可用程序来源，
  列出抽样日期和收盘，留给人工对公开网页核对；
- 建出的 CSV 复制到 logs/test/T3/行情/，结果写 logs/test/T3/核对.md。正式 data/ 不提交。
"""
import csv
import json
import random
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import probe_overseas as po  # noqa: E402

CODES = ["^IXIC", "^GSPC", "^N225", "^KS11", "^HSI", "hkHSTECH"]
SECOND = {"^IXIC": ("sina_us", ".IXIC"), "^GSPC": ("sina_us", ".INX"), "^HSI": ("tencent", "hkHSI")}
OUT = ROOT / "logs/test/T3"


def main():
    done = subprocess.run([sys.executable, "-u", str(ROOT / "scripts/update_hangqing.py"), "--codes", ",".join(CODES)])
    (OUT / "行情").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "logs/行情库_最新.md", OUT / "行情库_建库日志.md")
    names = {r["code"]: r["name"] for r in csv.DictReader((ROOT / "data/标的清单.csv").open(encoding="utf-8"))}
    lines = ["# T3 海外指数建库核对", "", f"运行：{datetime.now(po.BJ).isoformat(timespec='seconds')}（北京时间），GitHub Actions；"
             f"update_hangqing.py 退出码 {done.returncode}。", "",
             "|指数|代码|行数|起|止（当地）|最新收盘|", "|---|---|---:|---|---|---:|"]
    data, bad = {}, []
    for code in CODES:
        p = ROOT / "data/行情库/指数" / f"{code}.csv"
        if not p.exists():
            lines.append(f"|{names[code]}|{code}|缺文件|—|—|—|")
            bad.append(code)
            continue
        shutil.copy(p, OUT / "行情" / p.name)
        rows = list(csv.DictReader(p.open(encoding="utf-8")))
        data[code] = {r["date"]: float(r["close"]) for r in rows}
        lines.append(f"|{names[code]}|{code}|{len(rows)}|{rows[0]['date']}|{rows[-1]['date']}|{float(rows[-1]['close']):g}|")
        want = "2020-07-31" if code == "hkHSTECH" else "2010-01-08"
        if rows[0]["date"] > want or any(float(r["factor"]) != 1 for r in rows):
            bad.append(code)
    lines += ["", "## 抽 5 天收盘对照", ""]
    for code, closes in data.items():
        rnd = random.Random(20261101 + CODES.index(code))
        days = sorted(rnd.sample(sorted(closes), 5))
        if code in SECOND:
            src, sym = SECOND[code]
            try:
                other = {r[0].isoformat(): r[4] for r in po.clean(po.fetch(src, sym, date.today()))}
            except Exception as e:  # noqa: BLE001
                other, err = {}, str(e)
            lines += [f"**{names[code]}**（对照 {src}:{sym}）", "", "|日期|本库|对照源|相对差|结果|", "|---|---:|---:|---:|---|"]
            for d in days:
                o = other.get(d)
                diff = abs(closes[d] / o - 1) if o else None
                ok = diff is not None and diff <= 0.001
                bad += [] if ok else [f"{code} {d}"]
                lines.append(f"|{d}|{closes[d]:g}|{o if o else '缺'}|{'—' if diff is None else f'{diff:.4%}'}|{'一致' if ok else '不一致'}|")
        else:
            lines += [f"**{names[code]}**（无第二个程序来源，待人工对公开网页核对）", "", "|日期|本库收盘|", "|---|---:|"]
            lines += [f"|{d}|{closes[d]:g}|" for d in days]
        lines.append("")
    lines.append(f"不一致或缺失：{bad or '无'}")
    (OUT / "核对.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
