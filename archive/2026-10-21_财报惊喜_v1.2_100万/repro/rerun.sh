#!/bin/bash
# 原样复现本次回测，结果写到 rerun/
cd "$(dirname "$0")/.."
python3 repro/backtest.py --data "../../data/日线_2026-10-20_4" --strategy "repro/strategies/caibao_jingxi/cj_main.py" "repro/strategies/caibao_jingxi/cj_t3.py" "repro/strategies/caibao_jingxi/cj_t8.py" "repro/strategies/caibao_jingxi/cj_h20.py" "repro/strategies/caibao_jingxi/cj_h120.py" --capital 1000000 --out rerun
