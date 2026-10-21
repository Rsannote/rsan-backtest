import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "财报惊喜·主版本（超额5%·持60日·10只）"
REQUIRED = ["000300"]
REBALANCE = "daily"
WARMUP_DAYS = 0
MIN_TRADE_FRAC = 0.02
target_weights = core.make(threshold=0.05, hold_days=60, slots=10)
