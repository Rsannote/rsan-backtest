import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "缩量枯竭抄底(现金等待)"
REQUIRED = ["510300", "000300", "000001"]
REBALANCE = "daily"
WARMUP_DAYS = 0
target_weights = core.make("A", base=None)
