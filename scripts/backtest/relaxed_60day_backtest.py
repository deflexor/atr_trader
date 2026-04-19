"""60-Day Backtest with Relaxed Filters + Fixed Drawdown Bug

Changes made:
1. Fixed drawdown bug: now stores percentage, not dollars
2. Relaxed ATR filter: 0.0002 → 0.0001
3. Relaxed volume filter: 1.5x → 1.2x
4. Disabled pullback filter (too restrictive for 1m)
5. Using 60 days of data for more trades

Usage: uv run python scripts/backtest/relaxed_60day_backtest.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


async def main():
    symbol = "BTCUSDT"
    days = 60

    # 1. Fetch 60 days of 1m + 1h candles
    print(f"Fetching {days} days of {symbol} data from Bybit...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    # 1m candles
    print("  Fetching 1m candles...")
    raw_1m = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)
    print(f"  Got {len(raw_1m)} 1m candles")

    # 1h candles
    print("  Fetching 1h candles...")
    raw_1h = await adapter.fetch_ohlcv_paginated(symbol, "60", 1000, start_time, end_time)
    print(f"  Got {len(raw_1h)} 1h candles")

    # Parse 1m
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
        except:
            continue

    # Parse 1h
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
        except:
            continue

    # Deduplicate
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

    candles_1m = CandleSeries(result_1m, symbol=symbol, exchange="bybit", timeframe="1m")
    print(f"Using {len(candles_1m.candles)} 1m candles")
    print(f"Using {len(result_1h)} 1h candles")

    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    # 3. Configure strategy with RELAXED filters
    # Key changes:
    # - Lower ATR threshold (0.0001 vs 0.0002) - allow lower volatility
    # - Lower volume spike threshold (1.2 vs 1.5) - allow lower volume
    # - Disable pullback filter - too restrictive for 1m
    config = MomentumConfig(
        name="relaxed_60day",
        min_agreement=2,
        pullback_enabled=False,  # DISABLED - was blocking 46%
        volume_spike_threshold=1.2,  # Relaxed from 1.5
        atr_filter_min_pct=0.0001,  # Relaxed from 0.0002
        mtf_enabled=True,  # Keep H1Model
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    # 4. Configure backtest engine
    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        trailing_activation_atr=3.0,  # Best from optimization
        trailing_distance_atr=2.5,    # Best from optimization
        max_drawdown_pct=0.20,  # Enable 20% drawdown halt
        use_trailing_stop=True,
        cooldown_candles=4,
    )

    # 5. Create cached H1Model wrapper (same optimization as before)
    class CachedH1Model:
        def __init__(self, h1_model, candles_1h):
            self.h1_model = h1_model
            self.candles_1h = candles_1h
            self._cache = {}
            self._last_hour = -1

        def confirm_trend_cached(self, current_ts):
            from src.ml.model import CLASS_FLAT
            hour_bucket = int(current_ts.timestamp()) // 3600
            if hour_bucket != self._last_hour:
                self._last_hour = hour_bucket
                candle_series = CandleSeries(self.candles_1h)
                features = self.h1_model.feature_engine.create_features(candle_series, None)
                direction, confidence, probs = self.h1_model.model.predict_class(features)
                trend_agrees = direction != CLASS_FLAT and confidence > 0.4
                self._cache[hour_bucket] = (trend_agrees, direction, confidence, probs)
            return self._cache[hour_bucket]

    cached_h1 = CachedH1Model(h1_model, result_1h)

    original_confirm = h1_model.confirm_trend

    def cached_confirm(candles_1h, market_data=None):
        if candles_1h and len(candles_1h) > 0:
            current_ts = candles_1h[-1].timestamp
            return cached_h1.confirm_trend_cached(current_ts)
        return original_confirm(candles_1h, market_data)

    h1_model.confirm_trend = cached_confirm

    # 6. Run backtest
    print("\nRunning 60-day backtest with relaxed filters...")

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    engine = BacktestEngine(engine_config)
    start = datetime.now()
    result = await engine.run(candles_1m, signal_gen, 10000.0)
    duration = (datetime.now() - start).total_seconds()

    # 7. Print results
    print(f"\n{'='*60}")
    print("60-DAY BACKTEST RESULTS (RELAXED FILTERS)")
    print(f"{'='*60}")
    print(f"Duration:       {duration:.1f}s ({len(candles_1m.candles)} candles)")
    print(f"Initial:       ${result.initial_capital:.2f}")
    print(f"Final:         ${result.final_capital:.2f}")
    print(f"Return:        {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:  {result.max_drawdown:.2f}% <- FIXED (was storing $ as %)")
    print(f"Sharpe:        {result.sharpe_ratio:.2f}")
    print(f"Total Trades:  {result.total_trades}")
    print(f"Win Rate:      {result.win_rate:.1%}")

    # Trade analysis
    closed_trades = [t for t in result.trades if t.get("side") == "close" and t.get("pnl") is not None]
    winners = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losers = [t for t in closed_trades if t.get("pnl", 0) <= 0]
    print(f"\nClosed Trades: {len(closed_trades)}")
    print(f"Winners:       {len(winners)} ({len(winners)/len(closed_trades)*100:.1f}% if any)")
    print(f"Losers:        {len(losers)} ({len(losers)/len(closed_trades)*100:.1f}% if any)")

    # Diagnostics
    print(f"\n{'='*60}")
    print("FILTER DIAGNOSTICS")
    print(f"{'='*60}")
    total = strategy.diagnostics.get("total_evaluated", 1)
    filters = [
        ("atr_filtered", "ATR Volatility"),
        ("volume_filtered", "Volume Spike"),
        ("pullback_filtered", "Pullback"),
        ("h1_model_filtered", "H1Model"),
        ("rsi_divergence_filtered", "RSI Divergence"),
        ("entry_candle_filtered", "Entry Candle"),
    ]
    for key, name in filters:
        count = strategy.diagnostics.get(key, 0)
        if count > 0:
            pct = count / total * 100
            print(f"  {name:20}: {count:6} ({pct:5.1f}%)")

    print(f"\n  Signals produced: {strategy.diagnostics.get('signals_produced', 0)}")

    # Save results
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = Path(f"results/relaxed_60day_{date_str}.md")

    report = f"""# 60-Day Backtest Results (Relaxed Filters + Fixed Drawdown)

**Date**: {date_str}
**Symbol**: {symbol}
**Days**: {days}
**Candles**: {len(candles_1m.candles)} 1m + {len(result_1h)} 1h

## Configuration Changes

| Parameter | Before | After |
|----------|--------|-------|
| atr_filter_min_pct | 0.0002 | 0.0001 |
| volume_spike_threshold | 1.5 | 1.2 |
| pullback_enabled | True | False |
| trailing_activation_atr | 2.5 | 3.0 |
| trailing_distance_atr | 2.5 | 2.5 |

## Bug Fix

**Drawdown Bug Fixed**: The old code stored max drawdown in DOLLARS but reported as PERCENTAGE.
- Actual max drawdown is now calculated correctly: `($peak - $trough) / $peak * 100`

## Performance

| Metric | Value |
|--------|-------|
| Total Trades | {result.total_trades} |
| Win Rate | {result.win_rate:.1%} |
| Total Return | {result.total_return_pct:.2f}% |
| Max Drawdown | {result.max_drawdown:.2f}% (FIXED) |
| Sharpe Ratio | {result.sharpe_ratio:.2f} |
| Final Capital | ${result.final_capital:.2f} |
| Duration | {duration:.1f}s |

## Filter Block Rates

| Filter | Count | Pct |
|--------|-------|-----|
"""
    for key, name in filters:
        count = strategy.diagnostics.get(key, 0)
        if count > 0:
            pct = count / total * 100
            report += f"| {name} | {count} | {pct:.1f}% |\n"

    report += f"""
## Analysis

- ATR filter relaxed: now allows lower volatility environments
- Volume filter relaxed: now allows lower volume signals
- Pullback filter disabled: was too restrictive for 1m timeframe
- H1Model still active: filters conflicting 1h trend signals
- Drawdown bug fixed: now shows percentage, not dollars
"""
    output_file.write_text(report)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())