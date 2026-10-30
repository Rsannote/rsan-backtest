"""T6b 仅Actions核实已批准来源的解析结构与官方核对入口；接入完成后删除。"""
import json
import re
import time
import urllib.parse
from probe_macro import request, url

deadline = time.monotonic() + 400
sources = {
    "美联储H15": "https://www.federalreserve.gov/releases/h15/",
    "人行九月": "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026101417073743604/index.html",
    "统计局": "https://www.stats.gov.cn/sj/zxfb/",
    "统计局次页": "https://www.stats.gov.cn/sj/zxfb/index_1.html",
    "统计局CPI九月": "https://www.stats.gov.cn/sj/zxfb/202610/t20261014_1965477.html",
}
for name, source in sources.items():
    trace = []
    try:
        text = request(source, trace, deadline)
        if name == "外汇历史":
            print(name, text[:5000], flush=True)
        else:
            links = [(urllib.parse.urljoin(source, href), re.sub("<[^>]+>", "", title).strip())
                     for href, title in re.findall(r'<a\b[^>]*href=[\"\x27]([^\"\x27]+)[\"\x27][^>]*>(.*?)</a>', text, re.S|re.I)]
            print(name, json.dumps(links[:120], ensure_ascii=False), flush=True)
            print(name+'正文', re.sub(r'\s+', ' ', re.sub('<[^>]+>', ' ', text))[-22000:], flush=True)
            if name == "美债官方":
                from probe_macro import Tables
                table = Tables(); table.feed(text)
                print("美债行样例", json.dumps([r for r in table.rows if any("10-year" in c or "10 Yr" in c or "01/" in c for c in r)][:8]), flush=True)
        print("请求", json.dumps(trace, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(name, type(exc).__name__, str(exc), flush=True)
