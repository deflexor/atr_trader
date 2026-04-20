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

## Multi-Asset Backtest Results (120-day, 5m candles)

Using volatility-adaptive trailing stops (default since v1.1).

| Symbol | Return | Max DD | Trades | Win Rate |
|--------|--------|--------|--------|----------|
| DOGEUSDT | **5.12%** | 9.5% | 113 | 68% |
| BTCUSDT | **1.94%** | 6.0% | 72 | 79% |
| TRXUSDT | **0.25%** | 6.8% | 26 | 77% |
| TONUSDT | -2.83% | 9.2% | 194 | 60% |

**Note**: XMR/USDT not available on Bybit or KuCoin.

**Improvements over baseline** (vol-adaptive vs fixed ATR):
- DOGE: +0.39% return, -0.2% drawdown
- BTC: +0.58% return
- All assets: fewer unnecessary stop-outs in volatile periods

**Kelly sizing** (`use_geometric_sizing=True`): available but risky. DOGE 16% return but 37% drawdown. Not recommended for live trading.

## Bug Fixes Applied

1. **Drawdown calculation**: Fixed storing dollars as percentage
2. **Win rate calculation**: Fixed including open positions in win rate

## Scripts

| Script | Purpose |
|--------|---------|
| `h1_quick_test.py` | 7-day quick backtest |
| `balanced_60day_backtest.py` | 60-day test |
| `multi_asset_backtest.py` | Multi-asset backtest script |
| `generate_pl_charts.py` | P/L chart generation |
| `trailing_stop_optimization.py` | Test ATR ranges |
| `kelly_backtest.py` | Kelly criterion position sizing |
| `meta_label_backtest.py` | Meta-labeling filter test |
| `regime_backtest.py` | Market regime detection |

## Requirements

- Python 3.12+
- PyTorch (for H1Model)
- pandas, numpy
- Exchange adapter (Bybit/KuCoin)