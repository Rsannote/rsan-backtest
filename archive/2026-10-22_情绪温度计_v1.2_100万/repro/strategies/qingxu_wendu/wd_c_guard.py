import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "红利·过热避险"
REQUIRED = ["510880", "511260"]
REBALANCE = "daily"
WARMUP_DAYS = 0
target_weights = core.make_div_guard()
