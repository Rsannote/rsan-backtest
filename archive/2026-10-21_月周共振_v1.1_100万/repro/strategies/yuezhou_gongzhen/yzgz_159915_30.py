import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME = "月周共振·创业板ETF(宽松版回撤30%)"
REQUIRED = ["159915"]
REBALANCE = "weekly"
WARMUP_DAYS = 0  # 指标用窗口开始前的历史计算（数据远早于统一窗口起点），不含未来数据
target_weights = core.make("159915", 0.3)
