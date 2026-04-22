# Zero-Drawdown Risk Layer Comparison

**Date**: 2026-04-21
**Symbol**: BTCUSDT | **Days**: 60 | **Timeframe**: 5m

## Results

| Metric | Baseline | Zero-DD | Change |
|--------|----------|---------|--------|
| Max Drawdown | 8.99% | 9.04% | -0.05pp |
| Total Return | +2.10% | +1.42% | -0.68pp |
| Win Rate | 81.1% | 75.5% | |
| Sharpe | 4341.67 | 3609.39 | |
| Total Trades | 120 | 110 | |
| Closed Trades | 53 | 49 | |
| Winners | 43 | 37 | |

## Risk Layer Config

| Parameter | Value |
|-----------|-------|
| regime_lookback | 100 |
| boltzmann_temperature | 0.3 |
| bootstrap_confidence | 0.95 |
| per_trade_drawdown_budget | 1% |
| total_drawdown_budget | 3% |

## Risk Layer Diagnostics

{'regime_rejected': 12, 'budget_rejected': 0, 'worst_case_rejected': 0, 'boltzmann_reduced': 77, 'bootstrap_stop_overrides': 77}
