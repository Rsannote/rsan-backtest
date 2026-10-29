"""抓取美股席勒市盈率（CAPE，标普500，月度），存到 data/估值/美股CAPE.csv。

来源：multpl.com 的 Shiller PE 月度表（https://www.multpl.com/shiller-pe/table/by-month），
其数据来自耶鲁大学席勒教授的公开数据，最近几个月为 multpl 按最新价格和盈利估算。
用途：红利稳健配置·估值版 回测里判断纳指仓位的估值档位（只作美股整体估值的代理，不是纳指100本身的市盈率）。

用法：python3 scripts/fetch_us_cape.py
只用标准库和 pandas。数据只追加：已有月份的值不改写，变动另记日志。
"""
import html
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

URL = "https://www.multpl.com/shiller-pe/table/by-month"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "估值" / "美股CAPE.csv"
LOG = ROOT / "logs" / "美股CAPE_最新.md"
BJ = timezone(timedelta(hours=8))


def fetch():
    req = urllib.request.Request(URL, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", "replace")
    rows = re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", text, re.S)
    out = []
    for d, v in rows:
        d = re.sub(r"<[^>]+>", "", html.unescape(d)).strip()
        v = re.sub(r"<[^>]+>", "", html.unescape(v)).replace(" ", "").strip()
        try:
            dt = datetime.strptime(d, "%b %d, %Y").date()
            val = float(v)
        except ValueError:
            continue
        out.append((dt.isoformat(), val))
    return pd.DataFrame(out, columns=["date", "cape"]).drop_duplicates("date").sort_values("date")


def main():
    now = datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    try:
        new = fetch()
    except Exception as e:  # noqa: BLE001
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text(f"# 美股CAPE抓取\n\n{now} 失败：{e!r}\n", encoding="utf-8")
        print("抓取失败", e)
        sys.exit(1)
    if len(new) < 1000:
        LOG.write_text(f"# 美股CAPE抓取\n\n{now} 解析到的行数太少（{len(new)}），未写入。\n", encoding="utf-8")
        print("行数太少", len(new))
        sys.exit(1)
    new["来源"] = URL
    new["抓取时间"] = now
    OUT.parent.mkdir(parents=True, exist_ok=True)
    notes = []
    if OUT.exists():
        old = pd.read_csv(OUT, dtype={"date": str})
        merged = old.merge(new[["date", "cape"]], on="date", how="left", suffixes=("", "_新"))
        chg = merged[merged["cape_新"].notna() & (merged["cape"] - merged["cape_新"]).abs().gt(0.005)]
        for _, r in chg.iterrows():
            notes.append(f"- {r['date']}：已有 {r['cape']}，本次来源为 {r['cape_新']}（未改写）")
        add = new[~new["date"].isin(old["date"])]
        out = pd.concat([old, add], ignore_index=True).sort_values("date")
    else:
        add = new
        out = new
    out.to_csv(OUT, index=False)
    lines = [f"# 美股CAPE抓取", "", f"{now} 成功：来源 {len(new)} 行（{new['date'].iloc[0]} 至 {new['date'].iloc[-1]}），新增 {len(add)} 行。",
             f"最新一行：{new['date'].iloc[-1]} = {new['cape'].iloc[-1]}（multpl 对当月的估算值，月内会变）"]
    if notes:
        lines += ["", "来源与已存值不同的月份（按只追加规则未改写）："] + notes
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
