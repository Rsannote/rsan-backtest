#!/usr/bin/env python3
"""T1：仅在 Actions 探测新浪 ETF 概况页，不写正式行情库。"""
import re
import urllib.request
from pathlib import Path


def main():
    lines = ["T1 新浪 ETF 上市日期接口探测（仅在 GitHub Actions 执行）"]
    for code in ("512200", "516950", "159930", "159611"):
        url = f"https://finance.sina.com.cn/fund/quotes/{code}/bc.shtml"
        lines.append(f"\n代码 {code}；来源 {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as res:
                body = res.read()
                charset = res.headers.get_content_charset() or "gb18030"
            lines.append(f"返回字节数 {len(body)}；响应编码 {charset}")
            utf8 = body.decode("utf-8", errors="replace")
            if code == "512200":
                lines.append("脚本来源：" + "\n".join(re.findall(r'<script[^>]+src=[\"\x27]([^\"\x27]+)', utf8, re.I)))
                for m in re.finditer(r"ssrq|getFundInfo|FundInfo|[A-Za-z_]+_Service\.[A-Za-z_]+", utf8):
                    lines.append("接口线索：" + utf8[max(0, m.start()-180):m.end()+180])
            for encoding in dict.fromkeys((charset, "utf-8", "gb18030")):
                text = body.decode(encoding, errors="replace")
                clean = re.sub(r"<[^>]+>", " ", text)
                clean = re.sub(r"\s+", " ", clean)
                labels = list(re.finditer("上市|成立", clean))
                lines.append(f"解码 {encoding}；日期标签数 {len(labels)}；开头 {clean[:180]}")
                lines.extend(clean[max(0, m.start()-50):m.end()+180] for m in labels)
        except Exception as exc:
            lines.append(f"失败：{type(exc).__name__}: {exc}")
    out = Path("logs/test/T1/上市日期探测.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
