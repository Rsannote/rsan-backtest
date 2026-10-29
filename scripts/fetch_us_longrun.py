"""抓取美股长期参考研究用的月度数据，存到 data/估值/美股长期/。

用途：红利稳健配置·估值版 的长窗口参考研究（1970 年代起，覆盖两段美股失落十年）。
这批数据不是 ETF 行情，不进固定口径的回测引擎，只做参考研究。

来源（每个文件都带 来源 列）：
  标普500_席勒.csv  github.com/datasets/s-and-p-500（席勒教授公开数据的镜像）：月均价、分红、CPI、长期利率
  黄金_月度.csv      github.com/datasets/gold-prices：伦敦金月度价格（美元/盎司）
  十年国债_FRED.csv  FRED GS10：美国十年期国债收益率，月度
  CPI_FRED.csv       FRED CPIAUCSL：美国CPI，月度
  标普股息率.csv     multpl.com：标普500股息率，月度（补席勒数据 2023 年后缺的分红）
  标普价格.csv       multpl.com：标普500月度价格

用法：python3 scripts/fetch_us_longrun.py
只用标准库和 pandas。已存在的文件不覆盖，另存带日期的新文件并在日志里说明。
"""
import html
import io
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "估值" / "美股长期"
LOG = ROOT / "logs" / "美股长期数据_最新.md"
BJ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8", "replace")


def csv_src(url):
    return pd.read_csv(io.StringIO(get(url)))


def multpl(slug):
    url = f"https://www.multpl.com/{slug}/table/by-month"
    text = get(url)
    rows = re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", text, re.S)
    out = []
    for d, v in rows:
        d = re.sub(r"<[^>]+>", "", html.unescape(d)).strip()
        v = re.sub(r"<[^>]+>", "", html.unescape(v)).replace(" ", "").replace("%", "").replace(",", "").strip()
        try:
            out.append((datetime.strptime(d, "%b %d, %Y").date().isoformat(), float(v)))
        except ValueError:
            continue
    df = pd.DataFrame(out, columns=["date", "value"]).drop_duplicates("date").sort_values("date")
    return df, url


JOBS = {
    "标普500_席勒.csv": lambda: (csv_src(u := "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv"), u),
    "黄金_月度.csv": lambda: (csv_src(u := "https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv"), u),
    "十年国债_FRED.csv": lambda: (csv_src(u := "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GS10"), u),
    "CPI_FRED.csv": lambda: (csv_src(u := "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"), u),
    "标普股息率.csv": lambda: multpl("s-p-500-dividend-yield"),
    "标普价格.csv": lambda: multpl("s-p-500-price"),
}


def main():
    now = datetime.now(BJ)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    OUT.mkdir(parents=True, exist_ok=True)
    lines = ["# 美股长期数据抓取", "", f"运行时间 {stamp}", ""]
    fail = 0
    for name, job in JOBS.items():
        try:
            df, url = job()
            if len(df) < 100:
                raise ValueError(f"行数太少：{len(df)}")
            df["来源"] = url
            df["抓取时间"] = stamp
            path = OUT / name
            if path.exists():
                path = OUT / f"{path.stem}_{now.strftime('%Y%m%d')}.csv"
            df.to_csv(path, index=False)
            first, last = df.iloc[0, 0], df.iloc[-1, 0]
            lines.append(f"- {path.name}：成功，{len(df)} 行，{first} 至 {last}，来源 {url}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            lines.append(f"- {name}：失败 {e!r}")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if fail == len(JOBS):
        sys.exit(1)


if __name__ == "__main__":
    main()
