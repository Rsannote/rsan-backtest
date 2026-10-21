#!/bin/bash
# 原样复现本次回测，结果写到 rerun/
cd "$(dirname "$0")/.."
python3 repro/backtest.py --data "../../data/日线_2026-10-20_4" --strategy "repro/strategies/caibao_jingxi/cj_main5.py" --capital 20000 --out rerun
