#!/bin/bash
# 原样复现本次回测，结果写到 rerun/
cd "$(dirname "$0")/.."
python3 repro/backtest.py --data "../../data/日线_2026-10-28" --strategy "repro/strategies/hongli_guzhi/guzhi.py" --capital 1000000 --out rerun
