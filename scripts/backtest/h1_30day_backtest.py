"""Optimized 30-day backtest with H1Model.

Key optimizations:
1. Pre-fetch all data before backtest starts
2. Cache 1h candle features to avoid re-computation
3. Only update 1h confirmation once per hour (60 candles)
4. Disable logging during backtest for speed

Usage: uv run python scripts/backtest/h1_30day_backtest.py
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model
from src.ml.model import CLASS_FLAT

# Disable logging for speed
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)


async def fetch_all_data(symbol: str, days: int):
    """Pre-fetch all 1m and 1h candles before backtest."""
    print(f"Fetching {days} days of {symbol} data from Bybit...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    # Fetch 1m candles
    print("  Fetching 1m candles...")
    raw_1m = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)
    print(f"  Got {len(raw_1m)} 1m candles")

    # Fetch 1h candles
    print("  Fetching 1h candles...")
    raw_1h = await adapter.fetch_ohlcv_paginated(symbol, "60", 1000, start_time, end_time)
    print(f"  Got {len(raw_1h)} 1h candles")

    # Parse 1m candles
    candles_1m = []
    for r in raw_1m:
        try:
            ts = int(r[0]) // 1000
            candles_1m.append(Candle(
                symbol=symbol, exchange="bybit", timeframe="1m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])
            ))
        except Exception:
            continue

    # Parse 1h candles
    candles_1h = []
    for r in raw_1h:
        try:
            ts = int(r[0]) // 1000
            candles_1h.append(Candle(
                symbol=symbol, exchange="bybit", timeframe="1h",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])
            ))
        except Exception:
            continue

    # Deduplicate and sort
    seen_1m = {}
    result_1m = []
    for c in candles_1m:
        ts = int(c.timestamp.timestamp())
        if ts not in seen_1m:
            seen_1m[ts] = c
            result_1m.append(c)
    result_1m.sort(key=lambda x: x.timestamp.timestamp())

    seen_1h = {}
    result_1h = []
    for c in candles_1h:
        ts = int(c.timestamp.timestamp())
        if ts not in seen_1h:
            seen_1h[ts] = c
            result_1h.append(c)
    result_1h.sort(key=lambda x: x.timestamp.timestamp())

    return CandleSeries(result_1m, symbol=symbol, exchange="bybit", timeframe="1m"), result_1h


class CachedH1Model:
    """Wrapper around H1Model that caches 1h features per hour bucket."""

    def __init__(self, h1_model: H1Model, candles_1h: list):
        self.h1_model = h1_model
        self.candles_1h = candles_1h
        self._cache: dict[int, tuple] = {}  # hour_bucket -> (trend_agrees, direction, confidence, probs)
        self._last_hour = -1

    def confirm_trend_cached(self, current_ts: datetime) -> tuple:
        """Get cached H1Model result for current hour, computing if needed."""
        hour_bucket = int(current_ts.timestamp()) // 3600

        if hour_bucket != self._last_hour:
            self._last_hour = hour_bucket
            # Compute and cache (market_data not needed for 1h features)
            candle_series = CandleSeries(self.candles_1h)
            features = self.h1_model.feature_engine.create_features(candle_series, None)
            direction, confidence, probs = self.h1_model.model.predict_class(features)
            trend_agrees = direction != CLASS_FLAT and confidence > 0.4

            self._cache[hour_bucket] = (trend_agrees, direction, confidence, probs)

        return self._cache[hour_bucket]


async def main():
    symbol = "BTCUSDT"
    days = 30
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)

    # 1. Pre-fetch all data
    candles_1m, candles_1h = await fetch_all_data(symbol, days)
    print(f"Prepared {len(candles_1m.candles)} 1m candles")
    print(f"Prepared {len(candles_1h)} 1h candles")

    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")
    else:
        print("WARNING: H1Model not found!")

    # 3. Create cached H1Model wrapper
    cached_h1 = CachedH1Model(h1_model, candles_1h)

    # 4. Create strategy with hybrid config
    config = MomentumConfig(
        name="hybrid_30day",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
        # Kelly Criterion sizing (10% max position cap)
        kelly_sizing_enabled=True,
        kelly_max_pct=0.10,
        # Best trailing stop from optimization
        trailing_activation_atr=3.0,
        trailing_distance_atr=2.5,
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    # Override confirm_trend to use cached version
    original_confirm = h1_model.confirm_trend

    def cached_confirm(candles_1h, market_data=None):
        # Get current timestamp from candles to determine hour bucket
        if candles_1h and len(candles_1h) > 0:
            current_ts = candles_1h[-1].timestamp
            return cached_h1.confirm_trend_cached(current_ts)
        return original_confirm(candles_1h, market_data)

    h1_model.confirm_trend = cached_confirm

    # 5. Run backtest
    print("\nRunning 30-day backtest...")

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    engine = BacktestEngine(BacktestConfig(
        initial_capital=10000.0,
        use_trailing_stop=True,
        trailing_activation_atr=3.0,
        trailing_distance_atr=2.5,
    ))
    start = datetime.now()
    result = await engine.run(candles_1m, signal_gen, 10000.0)
    duration = (datetime.now() - start).total_seconds()

    # 6. Print results
    print(f"\n{'='*60}")
    print("30-DAY BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"Duration:      {duration:.1f}s ({len(candles_1m.candles)} candles)")
    print(f"Total Trades: {result.total_trades}")
    print(f"Win Rate:     {result.win_rate:.1%}")
    print(f"Return:       {result.total_return_pct:.2f}%")
    print(f"Max Drawdown: {result.max_drawdown:.2f}%")
    print(f"Sharpe:       {result.sharpe_ratio:.2f}")
    print(f"\nDiagnostics:")
    for k, v in strategy.diagnostics.items():
        if v > 0:
            print(f"  {k}: {v}")

    # 7. Save results
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = results_dir / f"hybrid_30day_{date_str}.md"

    report = f"""# Hybrid 30-Day Backtest Results

**Date**: {date_str}
**Symbol**: {symbol}
**Days**: {days}
**Candles**: {len(candles_1m.candles)} 1m + {len(candles_1h)} 1h

## Configuration

- Kelly Sizing: enabled (10% max position cap)
- Trailing Stop: activation=3.0 ATR, distance=2.5 ATR
- H1Model: enabled with caching
- Meta-labeling: disabled (needs 30-day training data)

## Performance

| Metric | Value |
|--------|-------|
| Total Trades | {result.total_trades} |
| Win Rate | {result.win_rate:.1%} |
| Total Return | {result.total_return_pct:.2f}% |
| Max Drawdown | {result.max_drawdown:.2f}% |
| Sharpe Ratio | {result.sharpe_ratio:.2f} |
| Final Capital | ${result.final_capital:.2f} |
| Duration | {duration:.1f}s |

## Diagnostics

| Filter | Count | Pct |
|--------|-------|-----|
"""
    total_eval = strategy.diagnostics.get("total_evaluated", 1)
    for k, v in strategy.diagnostics.items():
        if v > 0:
            report += f"| {k} | {v} | {v/total_eval*100:.1f}% |\n"

    report += f"""
## Analysis

- 30-day backtest with H1Model LSTM confirmation
- Cached 1h features (updated hourly)
- Win rate target: >50% for profitability
- Drawdown target: <20%
"""
    output_file.write_text(report)
    print(f"\nResults saved to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())