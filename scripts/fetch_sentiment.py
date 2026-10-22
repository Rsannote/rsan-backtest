"""每日社区情绪数据抓取：东财人气榜、飙升榜、雪球关注/讨论/交易榜。
每个数据源单独容错，一个失败不影响其他。结果存到 data/社区情绪/日期/ 下。
"""
import datetime as dt
import os
import sys

import akshare as ak

today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
out_dir = os.path.join("data", "社区情绪", today)
os.makedirs(out_dir, exist_ok=True)

sources = {
    "东财人气榜": lambda: ak.stock_hot_rank_em(),
    "东财飙升榜": lambda: ak.stock_hot_up_em(),
    "雪球关注榜": lambda: ak.stock_hot_follow_xq(symbol="最热门"),
    "雪球讨论榜": lambda: ak.stock_hot_tweet_xq(symbol="最热门"),
    "雪球交易榜": lambda: ak.stock_hot_deal_xq(symbol="最热门"),
}

ok, failed = [], []
for name, fn in sources.items():
    try:
        df = fn()
        if df is None or df.empty:
            raise ValueError("返回空数据")
        df.to_csv(os.path.join(out_dir, f"{name}.csv"), index=False, encoding="utf-8-sig")
        ok.append(f"{name}（{len(df)}行）")
    except Exception as e:  # noqa: BLE001
        failed.append(f"{name}：{type(e).__name__} {str(e)[:150]}")

with open(os.path.join(out_dir, "运行记录.txt"), "w", encoding="utf-8") as f:
    f.write(f"日期：{today}\n成功：{'、'.join(ok) or '无'}\n")
    f.write("失败：\n" + ("\n".join(failed) if failed else "无") + "\n")

print("成功：", ok)
print("失败：", failed)
sys.exit(0 if ok else 1)
