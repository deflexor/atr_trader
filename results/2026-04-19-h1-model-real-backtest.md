# H1Model Real Data Backtest Results

**Date**: 2026-04-19
**Feature**: Multi-timeframe LSTM (1m entry + 1h confirmation)
**Symbol**: BTCUSDT
**Candles**: 1500 (1m) - limited by KuCoin free API

## Results Comparison

| Metric | Baseline | H1Model-Enhanced |
|--------|----------|------------------|
| Total Trades | 2 | 2 |
| Win Rate | 50.0% | 50.0% |
| Return | -0.00% | -0.00% |
| Max Drawdown % | 150.19% | 150.19% |
| Sharpe Ratio | 0.00 | 0.00 |

## Diagnostics (H1Model-enhanced)
- total_evaluated: 1451
- atr_filtered: 1149 (79%) - volatility filter blocks most entries
- indicators_computed: 302
- min_agreement_passed: 267
- pullback_filtered: 262
- volume_filtered: 204
- signals_produced: 1

## Key Observations

1. **Data Limitation**: KuCoin free API only returns ~1500 1m candles (~24h). Not enough for meaningful backtest.

2. **Volatility Filter**: 79% of signals filtered by ATR volatility filter - strategy is selective.

3. **H1Model not filtering**: With only 26 1h candles (26 hours), H1Model may not have enough history for confirmation checks.

4. **Same results**: Both baseline and H1Model show identical results - H1Model's confirmation not triggering because not enough 1h data.

## Recommendations

1. **Use paid data or alternative source** for longer historical backtest (90+ days)
2. **Lower ATR filter threshold** to allow more entries in current market
3. **Increase 1h candle count** - currently 26 is insufficient for H1Model training/inference
4. **Consider using stored historical data** from existing candles.db

## Next Steps

1. Store 1m candles continuously to build history
2. Use 5m/15m aggregated data for longer historical analysis  
3. Tune ATR filter threshold for current volatility environment
