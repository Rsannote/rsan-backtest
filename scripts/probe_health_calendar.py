#!/usr/bin/env python3
"""仅在 Actions 探测健康检查所需的官方交易日历。"""
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", "联网探测只能在 GitHub Actions 运行"
    now = datetime.now(timezone(timedelta(hours=8)))
    nxt = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
    urls = [("深交所_本月", "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=" + now.strftime("%Y-%m")),
            ("深交所_次月", "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=" + nxt.strftime("%Y-%m")),
            ("上交所_交易日", "https://www.sse.com.cn/js/common/systemDate_global.js"),
            ("上交所_休市", "https://www.sse.com.cn/disclosure/dealinstruc/closed/")]
    out = Path("logs/test/T2/日历探测")
    out.mkdir(parents=True, exist_ok=True)
    summary = {"时间": now.isoformat(), "探测": []}
    for name, url in urls:
        item = {"来源": url, "名称": name}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/" if "szse" in url else "https://www.sse.com.cn/"})
            with urllib.request.urlopen(req, timeout=25) as res:
                raw = res.read()
                item["状态码"] = res.status
            text = raw.decode("utf-8", errors="replace")
            (out / (name + ".txt")).write_text(text[:100000], encoding="utf-8")
            item.update(字节数=len(raw), 成功=True, 开头=text[:800])
        except Exception as exc:
            item.update(成功=False, 错误=f"{type(exc).__name__}: {exc}")
        summary["探测"].append(item)
    (out / "结果.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
