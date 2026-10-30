# T13b 源API依据（阶段1）

2026-10-30通过GitHub插件读取并核实 [AkShare官方代码](https://github.com/akfamily/akshare/blob/5def33b2d0ba51529e386cb86d7da0c85e5a5626/akshare/stock/finance/stock_finance_ths.py)，blob SHA为 `5bc77c8204baeed2b1523e8f4f44a71c4e440388`。

确认签名为 `stock_financial_abstract_ths(symbol: str="000063", indicator: str="按报告期")`，indicator支持“按单季度”；实际发起 `GET https://basic.10jqka.com.cn/new/{symbol}/finance.html`，从p标签id=main读取JSON，title元素若为list则取首项；单季度用simple，首行是日期，余行是对应指标，转置后按报告期排序。仓库v3_select.py第177–178行使用同一函数及“按单季度”。新脚本仅用标准库HTMLParser替代bs4，保留同一数据字段和转置，不引入第三方财经库。

读取a-stock-data技能中相关财报索引/新浪三表说明，未发现可替代该同花顺单季度摘要的已记录函数，故不把累计新浪财报猜成单季数据。所有真实财经请求仅在Actions，阶段1本地只有GitHub文档读取和离线固定页面测试，未运行行情/财务HTTP。

真实GHA探测尚未运行；本阶段产物为3只探测的可运行分支工作流、离线解析与边界证据，不能把离线测试声称为真实接口覆盖。
