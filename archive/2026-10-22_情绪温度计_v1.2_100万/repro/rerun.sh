#!/bin/bash
# 原样复现本次回测，结果写到 rerun/
cd "$(dirname "$0")/.."
python3 repro/backtest.py --data "../../data/日线_2026-10-21" --strategy "repro/strategies/qingxu_wendu/wd_a_bond.py" "repro/strategies/qingxu_wendu/wd_b_div.py" "repro/strategies/qingxu_wendu/wd_c_guard.py" "repro/strategies/qingxu_wendu/hold_510300.py" --capital 1000000 --out rerun
