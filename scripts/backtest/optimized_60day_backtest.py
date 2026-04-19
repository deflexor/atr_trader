"""60-Day Backtest with FIXED Position Sizing and Exits

Changes:
1. Position size: 5% risk per trade (was 1.5%)
2. Trailing stop: 6 ATR activation (was 3 ATR) - let winners run
3. Fixed take profit: 4% TP (alternative to trailing)
4. Enable H1Model for confirmation
5. Fixed drawdown and win rate bugs (already applied)

Usage: uv run python scripts/backtest/optimized_60day_backtest.py
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
    timeframe = "5"

    # 1. Fetch data
    print(f"Fetching {days} days of {symbol} {timeframe}m data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)
    print(f"Got {len(raw)} {timeframe}m candles")

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
    print(f"Using {len(candles.candles)} candles")

    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    # 3. Configure with OPTIMIZED settings
    config = MomentumConfig(
        name="optimized_60day",
        min_agreement=2,
        pullback_enabled=False,
        volume_spike_threshold=1.0,  # Disable volume filter for more signals
        atr_filter_min_pct=0.00005,  # Lower threshold
        mtf_enabled=True,  # Enable H1Model
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    # 4. Configure engine with OPTIMIZED exits
    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.05,  # 5% risk (was 1.5%)
        trailing_activation_atr=6.0,  # Wider - let winners run (was 3.0)
        trailing_distance_atr=3.0,
        max_drawdown_pct=0.20,  # 20% halt
        use_trailing_stop=True,
        use_atr_stops=True,  # Enable fixed ATR stops too
        atr_sl_multiplier=2.0,  # 2 ATR stop loss
        atr_tp_multiplier=4.0,  # 4 ATR take profit (was off)
        cooldown_candles=4,
    )
    engine = BacktestEngine(engine_config)

    # 5. Run backtest
    print("\nRunning 60-day backtest with OPTIMIZED settings...")

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    engine = BacktestEngine(engine_config)
    start = datetime.now()
    result = await engine.run(candles, signal_gen, 10000.0)
    duration = (datetime.now() - start).total_seconds()

    # 6. Results
    print(f"\n{'='*60}")
    print("60-DAY BACKTEST - OPTIMIZED SETTINGS")
    print(f"{'='*60}")
    print(f"Duration:       {duration:.1f}s")
    print(f"Initial:       ${result.initial_capital:.2f}")
    print(f"Final:         ${result.final_capital:.2f}")
    print(f"Return:        {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:  {result.max_drawdown:.2f}%")
    print(f"Sharpe:        {result.sharpe_ratio:.2f}")
    print(f"Total Trades:  {result.total_trades}")
    print(f"Win Rate:      {result.win_rate:.1%}")

    # Trade analysis
    entries = [t for t in result.trades if t.get("side") in ["long", "short"]]
    closes = [t for t in result.trades if t.get("side") == "close"]
    closed_pnls = [t.get("pnl", 0) for t in closes if t.get("pnl") is not None]
    winners = [t for t in closes if t.get("pnl", 0) > 0]
    losers = [t for t in closes if t.get("pnl", 0) <= 0]

    print(f"\nClosed Trades: {len(closes)}")
    if closes:
        win_pnls = [t.get("pnl", 0) for t in winners]
        loss_pnls = [t.get("pnl", 0) for t in losers]
        print(f"Winners:       {len(winners)} ({len(winners)/len(closes)*100:.1f}%)")
        print(f"Losers:        {len(losers)} ({len(losers)/len(closes)*100:.1f}%)")
        print(f"Avg Win:       ${sum(win_pnls)/len(win_pnls):.2f}")
        print(f"Avg Loss:      ${sum(loss_pnls)/len(loss_pnls):.2f}")

    # Diagnostics
    print(f"\n{'='*60}")
    print("FILTER DIAGNOSTICS")
    print(f"{'='*60}")
    total = strategy.diagnostics.get("total_evaluated", 1)
    filters = [
        ("atr_filtered", "ATR"),
        ("volume_filtered", "Volume"),
        ("pullback_filtered", "Pullback"),
        ("h1_model_filtered", "H1Model"),
    ]
    for key, name in filters:
        count = strategy.diagnostics.get(key, 0)
        if count > 0:
            pct = count / total * 100
            print(f"  {name:15}: {count:6} ({pct:5.1f}%)")

    # Unrealized PnL
    last_price = candles.candles[-1].close
    open_pnl = 0
    for entry in entries:
        ep = entry.get("entry_price", 0)
        qty = entry.get("quantity", 0)
        if ep > 0 and qty > 0:
            if entry["side"] == "long":
                open_pnl += (last_price - ep) * qty
            else:
                open_pnl += (ep - last_price) * qty

    print(f"\n{'='*60}")
    print("POSITION ANALYSIS")
    print(f"{'='*60}")
    print(f"Open positions: {len(entries)}")
    print(f"Unrealized PnL: ${open_pnl:.2f}")
    print(f"Last price:     ${last_price:.2f}")

    # Close reason analysis
    print(f"\n{'='*60}")
    print("CLOSE REASON ANALYSIS")
    print(f"{'='*60}")
    reasons = {}
    for t in closes:
        reason = t.get("reason", "?")
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason:15}: {count}")

    # Save
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = Path(f"results/optimized_60day_{date_str}.md")

    report = f"""# 60-Day Backtest - OPTIMIZED Settings

**Date**: {date_str}
**Symbol**: {symbol}
**Days**: {days}
**Timeframe**: {timeframe}m
**Candles**: {len(candles.candles)}

## Configuration Changes

| Parameter | Old Value | New Value |
|-----------|-----------|-----------|
| risk_per_trade | 1.5% | 5.0% |
| trailing_activation_atr | 3.0 | 6.0 |
| volume_spike_threshold | 1.2 | 1.0 (disabled) |
| atr_filter_min_pct | 0.0001 | 0.00005 |
| atr_tp_multiplier | off | 4.0 |
| mtf_enabled | False | True |

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
| Closed Trades | {len(closes)} |
| Winners | {len(winners)} |
| Losers | {len(losers)} |
| Avg Win | ${sum(win_pnls)/len(win_pnls):.2f if win_pnls else 0} |
| Avg Loss | ${sum(loss_pnls)/len(loss_pnls):.2f if loss_pnls else 0} |

## Position Analysis

| Metric | Value |
|--------|-------|
| Open Positions | {len(entries)} |
| Unrealized PnL | ${open_pnl:.2f} |
| Last Price | ${last_price:.2f} |

## Close Reasons

"""
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        report += f"- {reason}: {count}\n"

    output_file.write_text(report)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())