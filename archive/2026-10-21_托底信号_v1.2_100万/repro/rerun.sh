#!/bin/bash
# 原样复现本次回测，结果写到 rerun/
cd "$(dirname "$0")/.."
python3 repro/backtest.py --data "../../data/日线_2026-10-20_3" --strategy "repro/strategies/tuodi_xinhao/td_cash20.py" "repro/strategies/tuodi_xinhao/td_cash60.py" "repro/strategies/tuodi_xinhao/td_cash20_optimistic.py" "repro/strategies/tuodi_xinhao/td_div20.py" "repro/strategies/tuodi_xinhao/hold_510300.py" --capital 1000000 --out rerun --start 2023-10-10 --end 2026-10-20
