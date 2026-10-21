#!/bin/bash
# 原样复现本次回测，结果写到 rerun/
cd "$(dirname "$0")/.."
python3 repro/backtest.py --data "../../data/日线_2026-10-20_5" --strategy "repro/strategies/liudong_kujie/kj_a_cash.py" "repro/strategies/liudong_kujie/kj_b_cash.py" "repro/strategies/liudong_kujie/kj_a_div.py" "repro/strategies/liudong_kujie/kj_b_div.py" "repro/strategies/liudong_kujie/hold_510300.py" --capital 1000000 --out rerun --end 2026-10-20
