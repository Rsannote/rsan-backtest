"""T6b 仅Actions核实已批准来源的解析结构与官方核对入口；接入完成后删除。"""
import json
import re
import time
import urllib.parse
from probe_macro import request, url

deadline = time.monotonic() + 400
sources = {
    "外汇历史": url("https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew", startDate="2015-01-01", endDate="2015-02-01", currency="USD/CNY", pageNum="1", pageSize="500"),
    "美债官方": url("https://home.treasury.gov/resource-center-data-chart-center/interest-rates/TextView", type="daily_treasury_yield_curve", field_tdr_date_value="2026"),
    "人民银行": "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html",
    "统计局": "https://www.stats.gov.cn/sj/zxfb/",
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
            selected = [x for x in links if any(k in str(x) for k in ("csv", "金融统计", "居民消费", "工业生产者", "采购经理", "PMI"))]
            print(name, json.dumps(selected[:35], ensure_ascii=False), flush=True)
            if name == "美债官方":
                from probe_macro import Tables
                table = Tables(); table.feed(text)
                print("美债行样例", json.dumps([r for r in table.rows if any("10-year" in c or "10 Yr" in c or "01/" in c for c in r)][:8]), flush=True)
        print("请求", json.dumps(trace, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(name, type(exc).__name__, str(exc), flush=True)
