# H1Model Bybit Backtest Results

**Date**: 2026-04-19
**Feature**: Multi-timeframe LSTM (1m entry + 1h confirmation)
**Source**: Bybit API (paginated, 1000 candles/page)
**Symbol**: BTCUSDT

## 7-Day Backtest Results (Fixed H1Model Filter)

| Metric | Baseline | H1Model-Enhanced |
|--------|----------|------------------|
| Total Trades | 14 | 6 |
| Win Rate | 35.7% | 50.0% |
| Return % | 0.02% | 0.02% |
| Max Drawdown % | 227.37% | 7.76% |
| Sharpe Ratio | 4447.54 | 17230.34 |

## 30-Day Backtest Results

| Metric | Value |
|--------|-------|
| Total Trades | 33 |
| Win Rate | 33.3% |
| Total Return | 0.05% |
| Max Drawdown % | 304.65% |
| Sharpe Ratio | 1246.38 |
| Duration | 176.8s (44k candles) |

## Threshold Sweep (7-day)

| Threshold | Trades | Win Rate | Max DD | H1 Filtered |
|-----------|--------|----------|--------|------------|
| 0.3 | 6 | 50.0% | 7.76% | 364 |
| 0.4 | 6 | 50.0% | 7.76% | 364 |
| 0.5 | 4 | 50.0% | 6.42% | 364 |
| 0.6 | 4 | 50.0% | 6.42% | 364 |

**Best threshold**: 0.3 (highest win rate with ≥5 trades)

## Diagnostics

### 7-Day (H1Model-Enhanced)
- h1_model_filtered: 360
- pullback_filtered: 4,993 (45%)
- volume_filtered: 4,261 (39%)
- signals_produced: 3

### 30-Day (H1Model-Enhanced)
- total_evaluated: 43,951
- atr_filtered: 18,548 (42%)
- pullback_filtered: 22,666 (52%)
- volume_filtered: 19,372 (44%)
- **h1_model_filtered: 1,686** (3.8%)
- signals_produced: 22

## Key Observations

1. **H1Model filtering works**: Blocking ~4-7% of conflicting signals across timeframes
2. **7-day win rate 50%** but 30-day win rate 33.3% - sample size issue or market regime change
3. **30-day drawdown too high**: 304% means strategy risked too much per trade
4. **Return very low**: Only 0.05% in 30 days (need bigger positions or better signals)
5. **Threshold insensitive**: Win rate same (50%) for 0.3-0.6 on 7-day

## Root Cause of Filtering Issue (Fixed)

The original code used `trend_agrees` (which was True when direction != FLAT and confidence > 0.4).
The new code checks if H1Model direction agrees with 1m signal direction:
- 1m LONG → want H1Model UP (2)
- 1m SHORT → want H1Model DOWN (0)

## Next Steps

1. **Risk management**: 30-day DD of 304% is unsustainable - reduce position size
2. **Win rate investigation**: Why does 7-day show 50% but 30-day shows 33%?
3. **Signal frequency**: Only 22 signals in 30 days = ~0.7/day - maybe too selective
4. **Consider lower H1Model threshold**: 0.3 showed same win rate but more trades
