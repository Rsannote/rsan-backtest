import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "红利底仓·缩量枯竭换沪深300"
REQUIRED = ["510300", "510880", "000300", "000001"]
REBALANCE = "daily"
WARMUP_DAYS = 0
target_weights = core.make("A", base="510880")
