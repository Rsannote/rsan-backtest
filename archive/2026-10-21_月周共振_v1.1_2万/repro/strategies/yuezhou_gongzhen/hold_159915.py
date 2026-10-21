import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "创业板ETF买入持有(对照)"
REQUIRED = ["159915"]
REBALANCE = "weekly"
WARMUP_DAYS = 0
target_weights = core.make_hold("159915")
