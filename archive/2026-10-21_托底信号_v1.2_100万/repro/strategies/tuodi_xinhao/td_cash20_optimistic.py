import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "托底跟买·沪深300(持20日·乐观版当日信号)"
REQUIRED = ["510300", "510880"]
REBALANCE = "daily"
WARMUP_DAYS = 0
target_weights = core.make(20, lag=False)
