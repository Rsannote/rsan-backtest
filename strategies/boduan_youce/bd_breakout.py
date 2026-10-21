import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "波段抄底·右侧突破版"
REQUIRED = ["000300"]
REBALANCE = "daily"
WARMUP_DAYS = 0
MIN_TRADE_FRAC = 0.02
target_weights = core.make("breakout")
