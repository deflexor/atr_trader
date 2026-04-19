"""60-Day Backtest with 5m candles for faster execution + relaxed filters + fixed drawdown

Using 5m candles instead of 1m:
- 60 days × 24 hours × 12 × 5min = ~17,280 candles vs 86,400 1m candles
- Much faster execution while maintaining signal quality
- More trades per day than 1m (less noise)

Changes made:
1. Fixed drawdown bug: now stores percentage, not dollars
2. Relaxed ATR filter: 0.0002 → 0.0001
3. Relaxed volume filter: 1.5x → 1.2x
4. Disabled pullback filter
5. Using 5m timeframe for speed

Usage: uv run python scripts/backtest/relaxed_60day_5m_backtest.py
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
    timeframe = "5"  # 5-minute candles for speed

    # 1. Fetch 60 days of 5m + 1h candles
    print(f"Fetching {days} days of {symbol} {timeframe}m data from Bybit...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    # 5m candles
    print("  Fetching 5m candles...")
    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)
    print(f"  Got {len(raw)} {timeframe}m candles")

    # Parse candles
    candles_list = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles_list.append(Candle(
                symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])
            ))
        except:
            continue

    # Deduplicate
    seen = {}
    result = []
    for c in candles_list:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    result.sort(key=lambda x: x.timestamp.timestamp())

    candles = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m")
    print(f"Using {len(candles.candles)} {timeframe}m candles")

    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    # 3. Configure strategy with RELAXED filters
    config = MomentumConfig(
        name="relaxed_60day_5m",
        min_agreement=2,
        pullback_enabled=False,  # DISABLED
        volume_spike_threshold=1.2,  # Relaxed from 1.5
        atr_filter_min_pct=0.0001,  # Relaxed from 0.0002
        mtf_enabled=False,  # Disable MTF for speed (5m doesn't need 1h confirmation)
    )
    strategy = MomentumStrategy(config=config, h1_model=None)

    # 4. Configure backtest engine
    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        trailing_activation_atr=3.0,
        trailing_distance_atr=2.5,
        max_drawdown_pct=0.20,  # Enable 20% drawdown halt
        use_trailing_stop=True,
        cooldown_candles=4,  # 4 × 5min = 20min cooldown
    )

    # 5. Run backtest
    print("\nRunning 60-day backtest...")

    async def signal_gen(sym, c):
        signal = await strategy.generate_signal(sym, c, None)
        return signal

    engine = BacktestEngine(engine_config)
    start = datetime.now()
    result = await engine.run(candles, signal_gen, 10000.0)
    duration = (datetime.now() - start).total_seconds()

    # 6. Print results
    print(f"\n{'='*60}")
    print("60-DAY BACKTEST RESULTS (5m, RELAXED FILTERS)")
    print(f"{'='*60}")
    print(f"Duration:       {duration:.1f}s ({len(candles.candles)} candles)")
    print(f"Initial:       ${result.initial_capital:.2f}")
    print(f"Final:         ${result.final_capital:.2f}")
    print(f"Return:        {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:  {result.max_drawdown:.2f}%")
    print(f"Sharpe:        {result.sharpe_ratio:.2f}")
    print(f"Total Trades:  {result.total_trades}")
    print(f"Win Rate:      {result.win_rate:.1%}")

    # Trade analysis
    closed_trades = [t for t in result.trades if t.get("side") == "close" and t.get("pnl") is not None]
    winners = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losers = [t for t in closed_trades if t.get("pnl", 0) <= 0]
    avg_win = sum(t.get("pnl", 0) for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t.get("pnl", 0) for t in losers) / len(losers) if losers else 0

    print(f"\nClosed Trades: {len(closed_trades)}")
    if closed_trades:
        print(f"Winners:       {len(winners)} ({len(winners)/len(closed_trades)*100:.1f}%)")
        print(f"Losers:        {len(losers)} ({len(losers)/len(closed_trades)*100:.1f}%)")
        print(f"Avg Win:       ${avg_win:.4f}")
        print(f"Avg Loss:      ${avg_loss:.4f}")

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
    output_file = Path(f"results/relaxed_60day_5m_{date_str}.md")

    report = f"""# 60-Day Backtest Results (5m timeframe + Relaxed Filters)

**Date**: {date_str}
**Symbol**: {symbol}
**Days**: {days}
**Timeframe**: {timeframe}m
**Candles**: {len(candles.candles)}

## Configuration

| Parameter | Value |
|-----------|-------|
| atr_filter_min_pct | 0.0001 |
| volume_spike_threshold | 1.2 |
| pullback_enabled | False |
| trailing_activation_atr | 3.0 |
| trailing_distance_atr | 2.5 |

## Bug Fix

**Drawdown Bug Fixed**: The old code stored max drawdown in DOLLARS but reported as PERCENTAGE.
- Now correctly shows: `($peak - $trough) / $peak * 100`

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

## Trade Analysis

| Metric | Value |
|--------|-------|
| Closed Trades | {len(closed_trades)} |
| Winners | {len(winners)} |
| Losers | {len(losers)} |
| Avg Win | ${avg_win:.4f} |
| Avg Loss | ${avg_loss:.4f} |

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

- Using 5m candles for faster execution (17k vs 86k candles)
- ATR filter relaxed: now allows lower volatility environments
- Volume filter relaxed: now allows lower volume signals
- Pullback filter disabled: was too restrictive
- No H1Model (disabled for speed)
"""
    output_file.write_text(report)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())