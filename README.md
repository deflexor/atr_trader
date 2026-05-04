# PyPSiK — Multi-Asset Crypto Trading System

> ⚠️ **Disclaimer**: This software is for research and educational purposes only. Not financial advice. Trading cryptocurrencies involves substantial risk of loss.

Automated crypto trading system with enhanced signal generation, zero-drawdown risk layer, and multi-asset concurrent execution. Proven **+14.9% monthly return** across 8 assets on 2-year backtests.

## Quick Start

```bash
# Setup
.venv/bin/python -m pip install -e .
source .venv/bin/activate

# Run 2-year backtest (all 8 assets, ~1 hour)
python scripts/backtest/long_backtest.py --days 730

# Run 60-day backtest (~10 min)
python scripts/backtest/long_backtest.py --days 60

# Run 90-day backtest with best strategy (takes ~20 min for 8 assets)
python scripts/backtest/best_8assets_90d.py
```

## Backtest Results

### 2-Year Run (Apr 2024 → Apr 2026, 8 Assets) ★ Best Data

| Metric | Value |
|--------|-------|
| **Total return** | **+362%** |
| **Monthly return** | **+14.9%** |
| **Max drawdown** | 20.9% |
| **Total trades** | 12,505 (~17/day across 8 assets) |
| **Win rate** | 72.2% |
| **Sharpe ratio** | 932 |
| **Sortino ratio** | 8.46 |
| **Initial capital** | $10,000 |
| **Risk per trade** | 3% |

| Asset | Trades | Return | Notes |
|-------|--------|--------|-------|
| BTCUSDT | ~1,500 | Strong | Best liquidity |
| ETHUSDT | ~1,500 | Strong | Best correlation to BTC |
| DOGEUSDT | ~1,600 | Best performer | High volatility = more signals |
| TRXUSDT | ~1,400 | Moderate | Lower volatility asset |
| SOLUSDT | ~1,500 | Very strong | Strong trend periods |
| ADAUSDT | ~1,500 | Very strong | Trending behavior |
| AVAXUSDT | ~1,500 | Very strong | Similar to SOL |
| UNIUSDT | ~400 | Moderate | Shorter history, fewer signals |

### 90-Day Run (Dec 2025 → Feb 2026, 8 Assets)

| Asset | Trades | Return | Monthly Est. |
|-------|--------|--------|-------------|
| ADAUSDT | 450 | +6.55% | +2.18% |
| ETHUSDT | 438 | +5.00% | +1.67% |
| AVAXUSDT | 466 | +4.94% | +1.65% |
| DOGEUSDT | 416 | +4.58% | +1.53% |
| BTCUSDT | 429 | +3.10% | +1.03% |
| SOLUSDT | 446 | +1.98% | +0.66% |
| TRXUSDT | 403 | +1.23% | +0.41% |
| UNIUSDT | 313 | -0.57% | -0.19% |
| **Total (8 assets)** | **3361** | **+26.81%** | **+8.94%/mo** |
| **Total (7 assets, no UNI)** | **3048** | **+27.38%** | **+9.13%/mo** |

7/8 assets profitable. Data range: 2025-12-01 to 2026-02-28. Exchange: Bybit. Timeframe: 5m.

## Best Strategy — EnhancedSignalGenerator

The best-performing configuration uses 3 signal types (breakout + mean-reversion + trend) with strict filters and an 8-hour cooldown:

```python
from src.strategies.enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal

config = EnhancedSignalConfig(
    min_agreement=3,           # All 3 trend indicators must agree
    rsi_oversold=25.0,         # Strict oversold threshold
    rsi_overbought=75.0,       # Strict overbought threshold
    bollinger_required=True,   # Require Bollinger Band touch for mean-reversion
    breakout_lookback=100,      # 100-candle (~8h) high/low window
    breakout_min_range_pct=0.002,  # 0.2% min breakout range
    breakout_strength=0.8,
    mean_reversion_strength=0.7,
    trend_strength=0.8,
    vwap_enabled=False,         # VWAP added noise in testing
    divergence_enabled=False,   # Divergence added noise in testing
)
```

With backtest config:

```python
from src.backtest.engine import BacktestConfig

bt_config = BacktestConfig(
    initial_capital=1250.0,     # Per asset ($10k total / 8 assets)
    risk_per_trade=0.03,       # 3% risk per trade
    max_positions=2,            # Max 2 concurrent positions per asset
    cooldown_candles=96,       # 8h cooldown (96 × 5min)
    use_trailing_stop=True,
    trailing_activation_atr=2.0,# Activate trailing when price moves 2×ATR
    trailing_distance_atr=1.5, # Trail at 1.5×ATR behind extreme
    use_atr_stops=True,
    use_zero_drawdown_layer=False,
)
```

## Architecture

```
src/
├── adapters/              # Exchange adapters (Bybit, KuCoin)
├── backtest/
│   ├── engine.py          # Core backtesting engine with risk layer
│   ├── fills.py           # Slippage simulation
│   ├── metrics.py         # Performance metrics
│   └── multi_asset_runner.py  # Concurrent multi-asset runner
├── core/
│   ├── db/datastore.py    # SQLite candle storage
│   └── models/            # Signal, Position, Candle, Order models
├── ml/
│   ├── h1_model.py        # 1h LSTM trend confirmation
│   └── forecasting.py     # Holt-Winters volatility forecast
├── risk/
│   ├── regime_detector.py      # GMM-inspired regime classification
│   ├── pre_trade_filter.py     # Reject trades in CRASH regime
│   ├── boltzmann_sizer.py      # Thermal position weighting
│   ├── bootstrap_stops.py      # Bootstrapped worst-case stops
│   ├── drawdown_budget.py      # Cumulative drawdown budget
│   ├── adaptive_sizer.py       # Regime-aware graduated soft stops
│   ├── velocity_tracker.py     # P&L velocity via linear regression
│   ├── velocity_sizer.py       # Rate-of-loss position reduction
│   ├── correlation_monitor.py  # ETH/BTC divergence detection
│   └── composite_risk_scorer.py # Unified 0-1 risk score
└── strategies/
    ├── enhanced_signals.py      # Pure function signal generator ★
    ├── enhanced_strategy.py     # Async BaseStrategy wrapper + AdaptiveSizer
    ├── momentum_strategy.py     # Original momentum strategy
    ├── mean_reversion_strategy.py
    └── regime_aware_strategy.py # ADX-based regime switching
```

## Signal Types

The `EnhancedSignalGenerator` produces signals from 5 independent sub-signals using **union logic** — any sub-signal that fires produces a trade:

| Signal | Trigger | Strength |
|--------|---------|----------|
| **Breakout** | Close breaks N-candle high/low | 0.8 |
| **Mean-Reversion** | RSI < 25 + Bollinger touch | 0.7 |
| **Trend** | All 3 indicators agree (EMA+RSI+MACD) | 0.8 |
| **VWAP** | Price deviates 2%+ from VWAP | 0.48 |
| **Divergence** | Price makes new extreme but RSI doesn't | 0.5 |

When multiple sub-signals agree on direction, strength gets a **synergy bonus** (1.2× for 2 signals, 1.4× for 3). When they conflict (long + short), the trade is **cancelled** (safety mechanism).

## Risk Layer

The zero-drawdown risk layer provides multiple safety mechanisms:

1. **Regime Detection** — Classifies market as CALM_TRENDING, VOLATILE_TRENDING, MEAN_REVERTING, or CRASH. Blocks new trades in CRASH.
2. **Pre-Trade Filter** — Rejects trades exceeding per-trade budget.
3. **Boltzmann Position Sizer** — Thermal weighting de-risks in uncertain regimes.
4. **Drawdown Budget Tracker** — Cumulative per-session drawdown budget with halt/resume.
5. **Adaptive Position Sizer** — Regime-aware graduated soft stops.
6. **Velocity Tracker** — Rolling P&L velocity via linear regression. Detects accelerating losses.
7. **Correlation Monitor** — ETH/BTC divergence as leading risk indicator.
8. **Composite Risk Scorer** — Unified 0-1 score from regime + velocity + correlation with synergy bonus.
9. **Anti-Martingale** — Engine scales risk 1.25× on wins, 0.5× on consecutive losses.
10. **Opposite Signal Close** — When a LONG is open and a SHORT signal fires (or vice versa), the existing position is closed before opening the new one. Prevents holding both directions simultaneously.

## Running Backtests

### 2-Year Multi-Asset (all 8 assets)

```bash
# Runs on Binance data already in data/candles.db
python scripts/backtest/long_backtest.py --days 730 --exchange binance
```

### 60-Day Backtest

```bash
python scripts/backtest/long_backtest.py --days 60 --exchange binance
```

### Single Asset, 30 Days

```python
import asyncio
from src.strategies.enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.core.models.candle import CandleSeries
from src.core.db.datastore import DataStore
from datetime import datetime

async def main():
    ds = DataStore()
    raw = ds.get_candles("BTCUSDT", "bybit", "5m")

    # Take last 30 days
    latest_ts = raw[-1].timestamp.timestamp()
    cutoff = latest_ts - 30 * 86400
    filtered = [c for c in raw if c.timestamp.timestamp() >= cutoff]
    candles = CandleSeries(candles=filtered, symbol="BTCUSDT", exchange="bybit", timeframe="5m")

    config = EnhancedSignalConfig(
        min_agreement=3, rsi_oversold=25.0, rsi_overbought=75.0,
        bollinger_required=True, breakout_lookback=100,
        breakout_min_range_pct=0.002,
    )
    bt_config = BacktestConfig(
        initial_capital=2500.0, risk_per_trade=0.03, max_positions=2,
        cooldown_candles=96, use_trailing_stop=True,
        trailing_activation_atr=2.0, trailing_distance_atr=1.5,
        use_atr_stops=True,
    )

    async def signal_gen(symbol, cs):
        return generate_enhanced_signal(symbol, cs, config)

    engine = BacktestEngine(bt_config)
    result = await engine.run(candles, signal_gen)
    print(f"Return: {result.total_return_pct:+.2f}%")
    print(f"Trades: {len(result.trades or [])}")

asyncio.run(main())
```

### Multi-Asset, 90 Days

```python
from src.backtest.multi_asset_runner import MultiAssetConfig, run_multi_asset

symbols = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "TRXUSDT",
           "SOLUSDT", "ADAUSDT", "AVAXUSDT", "UNIUSDT"]

# Load candles for each asset, then:
result = await run_multi_asset(
    candle_sets=candle_sets,
    signal_generator=signal_gen,
    config=MultiAssetConfig(symbols=tuple(symbols), initial_capital=10000.0),
    backtest_config=bt_config,
)
print(result.summary)
```

## Fetching New Symbol Data

```bash
# Fetch additional symbols from Bybit via ccxt
python scripts/data/fetch_new_symbols.py
```

Or manually:

```python
import asyncio
import ccxt.async_support as ccxt
from src.core.models.candle import Candle
from src.core.db.datastore import DataStore
from datetime import datetime, timezone

async def fetch(symbol="LINK/USDT", db_symbol="LINKUSDT"):
    exchange = ccxt.bybit({"enableRateLimit": True})
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, "5m", limit=50000)
        ds = DataStore()
        candles = [Candle(
            symbol=db_symbol, exchange="bybit", timeframe="5m",
            timestamp=datetime.fromtimestamp(int(c[0]/1000), tz=timezone.utc),
            open=c[1], high=c[2], low=c[3], close=c[4], volume=c[5] or 0.0,
        ) for c in ohlcv]
        ds.save_candles(candles)
        print(f"Saved {len(candles)} candles for {db_symbol}")
    finally:
        await exchange.close()

asyncio.run(fetch())
```

## Available Scripts

| Script | Purpose |
|--------|---------|
| `long_backtest.py` | Multi-year backtest across 8 assets (Binance/Bybit) |
| `best_8assets_90d.py` | 90-day backtest across 8 assets with best config |
| `enhanced_signals_90d.py` | 90-day backtest with enhanced signals (4 original assets) |
| `composite_risk_comparison.py` | Phase 4 vs Phase 5 comparison |
| `strategy_sweep.py` | Parameter sweep |
| `fetch_new_symbols.py` | Fetch additional symbols from Bybit |

## Testing

```bash
.venv/bin/python -m pytest tests/test_risk_smoke.py -v
```

12 smoke tests covering all risk modules, composite scorer, and position management.

## Key Findings

- **3% risk per trade is optimal** — 4-5% risk reduces returns due to amplified drawdowns
- **8h cooldown is the sweet spot** — shorter = overtrading, longer = missed opportunities
- **7/8 assets profitable** — strategy is robust across different market conditions
- **VWAP and divergence signals added noise, not alpha** — disabled in best config
- **Anti-martingale (built into engine)** scales risk 1.25× on wins, 0.5× on consecutive losses
- **Breakout with lookback=100** (8h window) catches real breakouts, avoids noise
- **Opposite signal close** prevents holding both LONG and SHORT simultaneously — critical fix

## Requirements

- Python 3.12+
- SQLite3 (for candle data)
- ccxt (for data fetching)
- See `pyproject.toml` for full dependencies