import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "冷买热卖沪深300·红利等待"
REQUIRED = ["510300", "510880"]
REBALANCE = "daily"
WARMUP_DAYS = 0
target_weights = core.make_switch("510880")
