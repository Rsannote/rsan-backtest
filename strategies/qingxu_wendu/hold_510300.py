import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "沪深300ETF买入持有(对照)"
REQUIRED = ["510300"]
REBALANCE = "daily"
WARMUP_DAYS = 0
target_weights = core.make_hold("510300")
