"""Debug the win rate calculation issue.

Why does backtest report 39.1% but actual is 88.8%?
The backtest reports total_trades (including open positions) but win_rate is calculated differently.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig

logging.basicConfig(level=logging.WARNING)


async def main():
    symbol = "BTCUSDT"
    days = 60
    timeframe = "5"

    print(f"Fetching {days} days of {timeframe}m data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)

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

    seen = {}
    result = []
    for c in candles_list:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    result.sort(key=lambda x: x.timestamp.timestamp())

    candles = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m")
    print(f"Using {len(candles.candles)} candles")

    config = MomentumConfig(
        name="debug",
        min_agreement=2,
        pullback_enabled=False,
        volume_spike_threshold=1.2,
        atr_filter_min_pct=0.0001,
        mtf_enabled=False,
    )
    strategy = MomentumStrategy(config=config, h1_model=None)

    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        trailing_activation_atr=3.0,
        trailing_distance_atr=2.5,
        max_drawdown_pct=0.20,
        use_trailing_stop=True,
        cooldown_candles=4,
    )
    engine = BacktestEngine(engine_config)

    async def signal_gen(sym, c):
        signal = await strategy.generate_signal(sym, c, None)
        return signal

    result = await engine.run(candles, signal_gen, 10000.0)

    print(f"\n{'='*60}")
    print("BACKTEST RESULT OBJECT")
    print(f"{'='*60}")
    print(f"total_trades:   {result.total_trades}")
    print(f"winning_trades: {result.winning_trades}")
    print(f"losing_trades:  {result.losing_trades}")
    print(f"win_rate:       {result.win_rate:.1%}")

    print(f"\n{'='*60}")
    print("ALL TRADES ANALYSIS")
    print(f"{'='*60}")
    print(f"Total trades in list: {len(result.trades)}")

    # Categorize trades
    entries = [t for t in result.trades if t.get("side") in ["long", "short"]]
    closes = [t for t in result.trades if t.get("side") == "close"]
    open_positions = [t for t in result.trades if t.get("side") == "open"]

    print(f"Entry trades:   {len(entries)}")
    print(f"Close trades:   {len(closes)}")
    print(f"Open positions: {len(open_positions)}")

    # Analyze closes
    closed_pnls = [t.get("pnl", 0) for t in closes if t.get("pnl") is not None]
    winners = [p for p in closed_pnls if p > 0]
    losers = [p for p in closed_pnls if p <= 0]

    print(f"\nClosed trades with PnL: {len(closed_pnls)}")
    print(f"Winners: {len(winners)} ({len(winners)/len(closed_pnls)*100:.1f}%)")
    print(f"Losers: {len(losers)} ({len(losers)/len(closed_pnls)*100:.1f}%)")

    # Check win_rate calculation in engine.py
    print(f"\n{'='*60}")
    print("WIN RATE CALCULATION")
    print(f"{'='*60}")
    print(f"BacktestResult.win_rate = {result.win_rate:.4f}")
    print(f"But actual from closed trades = {len(winners)/len(closed_pnls)*100:.1f}%")

    # Check what the engine is counting
    print(f"\nEngine counts:")
    print(f"  winning_trades: {result.winning_trades}")
    print(f"  losing_trades: {result.losing_trades}")
    print(f"  total_trades: {result.total_trades}")

    # Look at trade sides
    print(f"\nAll trade sides in result.trades:")
    sides = {}
    for t in result.trades:
        side = t.get("side", "?")
        sides[side] = sides.get(side, 0) + 1
    for side, count in sides.items():
        print(f"  {side}: {count}")


if __name__ == "__main__":
    asyncio.run(main())