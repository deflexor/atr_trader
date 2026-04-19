"""Analyze why returns are low despite high win rate.

Key issues:
1. Position sizing too small (1.5% per trade)
2. Trailing stop too tight (3 ATR = exits too fast)
3. Open positions not counted in PnL

This script shows the REAL return including open positions.
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
        name="analysis",
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
    print("OFFICIAL RESULTS (closed trades only)")
    print(f"{'='*60}")
    print(f"Final Capital:  ${result.final_capital:.2f}")
    print(f"Return:         {result.total_return_pct:.2f}%")
    print(f"Win Rate:       {result.win_rate:.1%}")

    # Analyze trades
    entries = [t for t in result.trades if t.get("side") in ["long", "short"]]
    closes = [t for t in result.trades if t.get("side") == "close"]

    print(f"\n{'='*60}")
    print("TRADE BREAKDOWN")
    print(f"{'='*60}")
    print(f"Total trades recorded: {len(result.trades)}")
    print(f"Entry trades (open):   {len(entries)}")
    print(f"Close trades:         {len(closes)}")

    # PnL from closed trades
    closed_pnls = [t.get("pnl", 0) for t in closes if t.get("pnl") is not None]
    closed_total = sum(closed_pnls)
    winners = [p for p in closed_pnls if p > 0]
    losers = [p for p in closed_pnls if p <= 0]

    print(f"\nClosed trade PnL:")
    print(f"  Winners: {len(winners)} × avg ${sum(winners)/len(winners):.4f} = ${sum(winners):.4f}")
    print(f"  Losers:  {len(losers)} × avg ${sum(losers)/len(losers):.4f} = ${sum(losers):.4f}")
    print(f"  Net from CLOSED trades: ${closed_total:.4f}")

    # Estimate unrealized PnL from open positions
    print(f"\n{'='*60}")
    print("OPEN POSITIONS (unrealized PnL)")
    print(f"{'='*60}")

    last_price = candles.candles[-1].close
    open_pnl = 0
    for entry in entries:
        entry_price = entry.get("entry_price", 0)
        quantity = entry.get("quantity", 0)
        if entry_price > 0 and quantity > 0:
            if entry["side"] == "long":
                pnl = (last_price - entry_price) * quantity
            else:  # short
                pnl = (entry_price - last_price) * quantity
            open_pnl += pnl

    print(f"Last candle price: ${last_price:.2f}")
    print(f"Estimated open positions: {len(entries)}")
    print(f"Estimated unrealized PnL: ${open_pnl:.4f}")

    # TOTAL return including open positions
    total_actual = closed_total + open_pnl
    total_return_pct = total_actual / 10000 * 100

    print(f"\n{'='*60}")
    print("REAL RETURN (closed + unrealized)")
    print(f"{'='*60}")
    print(f"Closed PnL:      ${closed_total:.4f}")
    print(f"Unrealized PnL:   ${open_pnl:.4f}")
    print(f"TOTAL PnL:        ${total_actual:.4f}")
    print(f"TOTAL RETURN:     {total_return_pct:.2f}%")
    print(f"TOTAL CAPITAL:    ${10000 + total_actual:.2f}")

    # WHY RETURNS ARE LOW
    print(f"\n{'='*60}")
    print("WHY RETURNS ARE LOW - ROOT CAUSE ANALYSIS")
    print(f"{'='*60}")

    print(f"\n1. POSITION SIZING:")
    print(f"   Risk per trade: 1.5% = $150 on $10,000")
    print(f"   Avg win: $1.95 = only 1.3% return per trade")
    print(f"   Even with 88.8% win rate, need many trades to accumulate")

    print(f"\n2. EXIT SPEED (Trailing Stop):")
    print(f"   Activation: 3 ATR - too tight!")
    print(f"   On 5m candles, 3 ATR = ~$150-300 moves")
    print(f"   Winners exit fast, don't get to run")

    print(f"\n3. TRADE FREQUENCY:")
    trades_per_day = len(closes) / 60
    print(f"   Closed trades/day: {trades_per_day:.1f}")
    print(f"   If avg win = $1.95 and {trades_per_day:.1f}/day:")
    print(f"   Daily gross = ${1.95 * trades_per_day:.2f}")
    print(f"   Monthly gross = ${1.95 * trades_per_day * 30:.2f}")
    print(f"   That's only {1.95 * trades_per_day * 30 / 10000 * 100:.1f}% monthly!")

    print(f"\n4. SOLUTION:")
    print(f"   a) Increase position size: 1.5% → 5-10%")
    print(f"   b) Wider trailing stop: 3 ATR → 6-10 ATR")
    print(f"   c) Let winners run longer")


if __name__ == "__main__":
    asyncio.run(main())