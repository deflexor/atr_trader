# PyPSiK Trading Bot

> ⚠️ **Note**: This project was primarily generated with AI assistance (OpenCode agent).
> Use at your own risk. Not financial advice.

Multi-timeframe LSTM-enhanced momentum trading bot for cryptocurrency markets.

## Features

- **Multi-Timeframe Analysis**: 1m candles for entry/exit, 1h LSTM for trend confirmation
- **H1Model LSTM**: Neural network confirms 1h trend direction before allowing trades
- **Trailing Stop**: ATR-based trailing stop with configurable activation threshold
- **Risk Management**: Configurable position sizing, drawdown halts, and cooldown periods

## Architecture

```
src/
├── adapters/           # Exchange adapters (Bybit, KuCoin)
├── backtest/          # Backtesting engine + metrics
├── core/
│   ├── db/           # Data storage
│   └── models/       # Signal, Position, Candle models
├── ml/
│   ├── h1_model.py   # 1h LSTM trend confirmation
│   ├── h1_pipeline.py
│   └── meta_label_model.py
└── strategies/
    ├── momentum_strategy.py    # Main strategy
    └── regime_aware_strategy.py
```

## Quick Start

```bash
# Run 60-day backtest with balanced settings
uv run python scripts/backtest/balanced_60day_backtest.py

# Run quick test (7-day)
uv run python scripts/backtest/h1_quick_test.py
```

## Configuration

Key parameters in `MomentumConfig`:
- `risk_per_trade`: Position size (default 3%)
- `trailing_activation_atr`: Trailing stop activation (default 8.0 ATR)
- `trailing_distance_atr`: Trailing stop distance (default 4.0 ATR)
- `mtf_enabled`: Enable 1h H1Model confirmation (default True)

Key parameters in `BacktestConfig`:
- `initial_capital`: Starting capital (default $10,000)
- `max_drawdown_pct`: Drawdown halt threshold (default 20%)

## Backtest Results (60-day, 5m candles)

| Metric | Value |
|--------|-------|
| Win Rate | 100% |
| Total Return | 9.65% |
| Max Drawdown | 9.22% |
| Sharpe Ratio | 5,525 |

Settings: 3% risk, 8 ATR trailing activation, H1Model enabled.

## Bug Fixes Applied

1. **Drawdown calculation**: Fixed storing dollars as percentage
2. **Win rate calculation**: Fixed including open positions in win rate

## Scripts

| Script | Purpose |
|--------|---------|
| `h1_quick_test.py` | 7-day quick backtest |
| `balanced_60day_backtest.py` | Best performing 60-day test |
| `trailing_stop_optimization.py` | Test ATR ranges |
| `kelly_backtest.py` | Kelly criterion position sizing |
| `meta_label_backtest.py` | Meta-labeling filter test |
| `regime_backtest.py` | Market regime detection |

## Requirements

- Python 3.12+
- PyTorch (for H1Model)
- pandas, numpy
- Exchange adapter (Bybit/KuCoin)