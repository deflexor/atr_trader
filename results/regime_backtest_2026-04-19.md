# Market Regime Detection Backtest Results

Generated: 2026-04-19T07:43:56.851573+00:00

## Configuration
- Symbol: BTCUSDT
- Backtest Period: 7 days
- Initial Capital: 10,000 USDT
- ADX Threshold: 25 (ADX > 25 = TRENDING, <= 25 = RANGING)

## Strategy Selection
- TRENDING (ADX > 25): Momentum strategy (trend following)
- RANGING (ADX <= 25): Mean Reversion strategy (fade the move)

## Results Comparison

| Metric | Momentum | Regime-Aware | Difference |
|---|---|---|---|
| Total Trades | 6 | 506 | +500 |
| Win Rate | 50.0% | 16.2% | -33.8% |
| Max Drawdown | 7.76% | 200.58% | +192.82% |
| Sharpe Ratio | 17998.33 | 995.97 | -17002.36 |
| Return | 0.02% | -0.03% | -0.04% |
| Final Capital | $10001.56 | $9997.32 | $-4.24 |

## Interpretation
- Regime-aware adapts to market conditions by selecting appropriate strategy
- If regime-aware outperforms, it suggests market regime switches are exploitable