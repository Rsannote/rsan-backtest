#!/usr/bin/env python3
"""探测市场简报要用的数据源在 GitHub 服务器上能不能用（只探测，不入库）。
结果：logs/explore/brief/<名称>.txt 与 汇总.json。用法：python3 scripts/explore_brief.py
"""
import datetime as dt
import json
import os
import time
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "explore", "brief")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
d = (dt.datetime.utcnow() + dt.timedelta(hours=8) - dt.timedelta(days=1))
while d.weekday() >= 5:
    d -= dt.timedelta(days=1)
D = d.strftime("%Y%m%d")
UT = "7eea3edcaed734bea9cbfc24409ed989"

SRC = {
    "东财_涨停池": f"https://push2ex.eastmoney.com/getTopicZTPool?ut={UT}&dpt=wz.ztzt&Pageindex=0&pagesize=320&sort=fbt%3Aasc&date={D}",
    "东财_跌停池": f"https://push2ex.eastmoney.com/getTopicDTPool?ut={UT}&dpt=wz.ztzt&Pageindex=0&pagesize=320&sort=fund%3Aasc&date={D}",
    "东财_炸板池": f"https://push2ex.eastmoney.com/getTopicZBPool?ut={UT}&dpt=wz.ztzt&Pageindex=0&pagesize=320&sort=fbt%3Aasc&date={D}",
    "东财_两融": "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ&columns=ALL&source=WEB&sortColumns=DIM_DATE&sortTypes=-1&pageNumber=1&pageSize=10",
    "东财_行情clist": "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&fs=m:1+t:2&fields=f12,f14,f2,f3,f6",
    "东财_沪深成交": "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001&fields=f12,f14,f2,f3,f6",
    "腾讯_指数实时": "https://qt.gtimg.cn/q=sh000001,sz399001,sh000300",
    "腾讯_指数日K": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,10,",
    "新浪_指数日K": "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x=/CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=5",
    "新浪_涨跌家数": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a",
    "新浪_涨停列表": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=1&num=80&sort=changepercent&asc=0&node=hs_a",
    "上交所_融资": "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do?isPagination=true&pageHelp.pageSize=5&beginDate=&endDate=",
    "深交所_融资": "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1837_xxpl&TABKEY=tab1",
    "同花顺_涨停": f"https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?page=1&limit=15&field=199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004&filter=HS,GEM2STAR&order_field=330324&order_type=0&date={D}",
    "同花顺_涨跌停统计": f"https://data.10jqka.com.cn/dataapi/limit_up/block_top?filter=HS,GEM2STAR&date={D}",
    "金十_美债": "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1",
    "十年国债_中债": "https://yield.chinabond.com.cn/cbweb-czb-web/czb/moreInfo?locale=cn_ZH&nameType=1",
}


def main():
    os.makedirs(OUT, exist_ok=True)
    summ = {}
    for k, u in SRC.items():
        h = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/" if "eastmoney" in u else "https://www.sse.com.cn/" if "sse" in u else "https://finance.sina.com.cn/"}
        if "jin10" in u:
            h.update({"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=25) as r:
                raw = r.read()
            res = {"ok": True, "bytes": len(raw), "s": round(time.time() - t0, 2)}
        except Exception as e:
            raw, res = b"", {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "s": round(time.time() - t0, 2)}
        summ[k] = res
        txt = raw[:3000].decode("utf-8", errors="replace")
        if "gtimg" in u and "q=" in u:
            txt = raw[:3000].decode("gbk", errors="replace")
        with open(os.path.join(OUT, k + ".txt"), "w", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n" + u + "\n\n" + txt)
        print("通" if res["ok"] else "不通", k, res)
    with open(os.path.join(OUT, "汇总.json"), "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
