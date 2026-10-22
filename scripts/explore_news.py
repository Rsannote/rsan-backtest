#!/usr/bin/env python3
"""探测新闻/公告/政策数据源在 GitHub 服务器上能不能用（只探测，不入库）。

每个源抓一次，把状态、耗时和返回内容前 4000 字节存到 logs/explore/news/<名称>.txt，
汇总写 logs/explore/news/汇总.json。用法：python3 scripts/explore_news.py
"""
import json
import os
import time
import urllib.parse
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "explore", "news")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

GET = {
    "新浪_7x24直播": ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50&zhibo_id=152&tag_id=0&dire=f&dpc=1&type=0", {}),
    "新浪_财经滚动": ("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=50&page=1", {}),
    "新浪_证券滚动": ("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2517&k=&num=50&page=1", {}),
    "财联社_电报": ("https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=8.4.6&rn=50", {"Referer": "https://www.cls.cn/telegraph"}),
    "财联社_电报2": ("https://www.cls.cn/nodeapi/updateTelegraphList?app=CailianpressWeb&os=web&sv=8.4.6&rn=50&hasFirstVipArticle=1", {"Referer": "https://www.cls.cn/telegraph"}),
    "华尔街见闻_快讯": ("https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=a-stock-channel&client=pc&limit=50", {}),
    "华尔街见闻_全球": ("https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&client=pc&limit=50", {}),
    "东财_快讯": ("https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html", {}),
    "东财_7x24": ("https://np-weblist.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=50&req_trace=1", {}),
    "金十_快讯": ("https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1", {"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0", "Origin": "https://www.jin10.com", "Referer": "https://www.jin10.com/"}),
    "同花顺_快讯": ("https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=50", {}),
    "第一财经_快讯": ("https://www.yicai.com/api/ajax/getlatest?page=1&pagesize=30", {}),
    "国务院_最新政策": ("https://www.gov.cn/zhengce/zuixin/", {}),
    "国务院_政策JSON": ("https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json", {}),
    "人民银行_新闻": ("http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html", {}),
    "证监会_要闻": ("http://www.csrc.gov.cn/csrc/c100028/common_list.shtml", {}),
    "发改委_新闻": ("https://www.ndrc.gov.cn/xwdt/xwfb/", {}),
    "统计局_最新发布": ("https://www.stats.gov.cn/sj/zxfb/", {}),
    "巨潮_搜索代码": ("http://www.cninfo.com.cn/new/information/topSearch/query?keyWord=600900&maxNum=5", {}),
    "上交所_公告": ("https://query.sse.com.cn/security/stock/queryCompanyBulletin.do?isPagination=true&productId=600900&pageHelp.pageSize=10&pageHelp.pageNo=1", {"Referer": "https://www.sse.com.cn/"}),
}

POST = {
    "巨潮_公告查询": ("http://www.cninfo.com.cn/new/hisAnnouncement/query",
                   {"pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext", "plate": "",
                    "stock": "", "searchkey": "", "secid": "", "category": "", "trade": "",
                    "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true"}),
}


def fetch(url, headers=None, data=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data else None
    if body:
        h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=25) as r:
            raw = r.read()
            return {"ok": True, "status": r.status, "bytes": len(raw), "s": round(time.time() - t0, 2)}, raw
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "s": round(time.time() - t0, 2)}, b""


def main():
    os.makedirs(OUT, exist_ok=True)
    summary = {}
    items = [(k, u, h, None) for k, (u, h) in GET.items()] + [(k, u, {}, d) for k, (u, d) in POST.items()]
    for name, url, h, d in items:
        r, raw = fetch(url, h, d)
        summary[name] = r
        txt = raw[:4000].decode("utf-8", errors="replace")
        with open(os.path.join(OUT, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n" + url + "\n\n" + txt)
        print("通" if r["ok"] else "不通", name, r)
    with open(os.path.join(OUT, "汇总.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
