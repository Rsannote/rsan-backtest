import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "红利底仓·托底换沪深300(20日)"
REQUIRED = ["510300", "510880"]
REBALANCE = "daily"
WARMUP_DAYS = 0
target_weights = core.make(20, lag=True, base="510880")
