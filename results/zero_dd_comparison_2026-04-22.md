# Zero-Drawdown Risk Layer Comparison

**Date**: 2026-04-22
**Symbol**: BTCUSDT | **Days**: 60 | **Timeframe**: 5m

## Results

| Metric | Baseline | Zero-DD | Change |
|--------|----------|---------|--------|
| Max Drawdown | 8.99% | 9.04% | -0.05pp |
| Total Return | +2.04% | +1.42% | -0.61pp |
| Win Rate | 80.8% | 75.5% | |
| Sharpe | 4372.85 | 3764.42 | |
| Total Trades | 118 | 110 | |
| Closed Trades | 52 | 49 | |
| Winners | 42 | 37 | |

## Risk Layer Config

| Parameter | Value |
|-----------|-------|
| regime_lookback | 100 |
| boltzmann_temperature | 0.3 |
| bootstrap_confidence | 0.95 |
| per_trade_drawdown_budget | 1% |
| total_drawdown_budget | 3% |

## Risk Layer Diagnostics

{'regime_rejected': 11, 'budget_rejected': 0, 'worst_case_rejected': 0, 'boltzmann_reduced': 77, 'vol_spike_reduced': 1}
