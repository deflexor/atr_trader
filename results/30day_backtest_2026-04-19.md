# 30-Day H1Model Backtest Results

**Date**: 2026-04-19
**Symbol**: BTCUSDT
**Days**: 30
**Candles**: 44000 1m + 1000 1h

## Performance

| Metric | Value |
|--------|-------|
| Total Trades | 33 |
| Win Rate | 33.3% |
| Total Return | 0.05% |
| Max Drawdown | 304.65% |
| Sharpe Ratio | 1246.38 |
| Duration | 176.8s |

## Diagnostics

| Filter | Count | Pct |
|--------|-------|-----|
| total_evaluated | 43951 | 100.0% |
| atr_filtered | 18548 | 42.2% |
| indicators_computed | 25403 | 57.8% |
| min_agreement_passed | 23054 | 52.5% |
| h1_model_filtered | 1686 | 3.8% |
| pullback_filtered | 22666 | 51.6% |
| signals_produced | 22 | 0.1% |
| volume_filtered | 19372 | 44.1% |

## Analysis

- 30-day backtest with H1Model LSTM confirmation
- Cached 1h features (updated hourly)
- Win rate target: >50% for profitability
- Drawdown target: <20%
