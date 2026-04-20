"""30-Day Backtest - 1m Timeframe (Balanced Settings)

Testing 1m vs 5m with same period (30 days) to compare:
- ~43,200 candles (vs ~8,640 for 5m)
- Higher granularity signals
- More noise but potentially better entry timing

Usage: uv run python scripts/backtest/balanced_30day_1m_backtest.py
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
    days = 30
    timeframe = "1"

    print(f"Fetching {days} days of {symbol} {timeframe}m data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)
    print(f"Got {len(raw)} {timeframe}m candles")

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

    # Load H1Model
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    # BALANCED config: moderate risk, wide trailing, no fixed TP
    config = MomentumConfig(
        name="balanced_30day_1m",
        min_agreement=2,
        pullback_enabled=False,
        volume_spike_threshold=1.0,  # Disabled
        atr_filter_min_pct=0.00005,  # Very low
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.03,  # 3% risk (moderate)
        trailing_activation_atr=8.0,  # WIDE - let winners run (8 ATR!)
        trailing_distance_atr=4.0,  # 4 ATR distance
        max_drawdown_pct=0.20,
        use_trailing_stop=True,
        use_atr_stops=False,  # NO fixed stops - only trailing
        cooldown_candles=6,  # Longer cooldown
    )
    engine = BacktestEngine(engine_config)

    print("\nRunning 30-day backtest with BALANCED settings (1m timeframe)...")

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    start = datetime.now()
    result = await engine.run(candles, signal_gen, 10000.0)
    duration = (datetime.now() - start).total_seconds()

    print(f"\n{'='*60}")
    print("30-DAY BACKTEST - BALANCED SETTINGS (1m)")
    print(f"{'='*60}")
    print(f"Duration:       {duration:.1f}s")
    print(f"Initial:       ${result.initial_capital:.2f}")
    print(f"Final:         ${result.final_capital:.2f}")
    print(f"Return:        {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:  {result.max_drawdown:.2f}%")
    print(f"Sharpe:        {result.sharpe_ratio:.2f}")
    print(f"Total Trades:  {result.total_trades}")
    print(f"Win Rate:      {result.win_rate:.1%}")

    entries = [t for t in result.trades if t.get("side") in ["long", "short"]]
    closes = [t for t in result.trades if t.get("side") == "close"]
    winners = [t for t in closes if t.get("pnl", 0) > 0]
    losers = [t for t in closes if t.get("pnl", 0) <= 0]

    print(f"\nClosed Trades: {len(closes)}")
    if closes:
        win_pnls = [t.get("pnl", 0) for t in winners]
        loss_pnls = [t.get("pnl", 0) for t in losers]
        avg_win = sum(win_pnls)/len(win_pnls) if win_pnls else 0
        avg_loss = sum(loss_pnls)/len(loss_pnls) if loss_pnls else 0
        print(f"Winners:       {len(winners)} ({len(winners)/len(closes)*100:.1f}%)")
        print(f"Losers:        {len(losers)} ({len(losers)/len(closes)*100:.1f}%)")
        print(f"Avg Win:       ${avg_win:.2f}")
        print(f"Avg Loss:      ${avg_loss:.2f}")

    # Unrealized
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

    print(f"\nOpen positions: {len(entries)}")
    print(f"Unrealized PnL: ${open_pnl:.2f}")
    print(f"Last price:     ${last_price:.2f}")

    # Close reasons
    reasons = {}
    for t in closes:
        reason = t.get("reason", "?")
        reasons[reason] = reasons.get(reason, 0) + 1
    print(f"\nClose reasons:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")

    # TOTAL return including open positions
    closed_pnl = sum(t.get("pnl", 0) for t in closes if t.get("pnl") is not None)
    total_pnl = closed_pnl + open_pnl
    total_return = total_pnl / 10000 * 100

    print(f"\n{'='*60}")
    print("REAL RETURN (closed + unrealized)")
    print(f"{'='*60}")
    print(f"Closed PnL:     ${closed_pnl:.2f}")
    print(f"Unrealized PnL:  ${open_pnl:.2f}")
    print(f"TOTAL PnL:       ${total_pnl:.2f}")
    print(f"TOTAL RETURN:   {total_return:.2f}%")

    # Save
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = Path(f"results/balanced_30day_1m_{date_str}.md")

    report = f"""# 30-Day Backtest - BALANCED Settings (1m Timeframe)

**Date**: {date_str}
**Symbol**: {symbol}
**Days**: {days}
**Timeframe**: {timeframe}m

## Configuration

| Parameter | Value |
|-----------|-------|
| risk_per_trade | 3% |
| trailing_activation_atr | 8.0 |
| trailing_distance_atr | 4.0 |
| use_atr_stops | False |
| mtf_enabled | True |

## Performance

| Metric | Value |
|--------|-------|
| Total Trades | {result.total_trades} |
| Win Rate | {result.win_rate:.1%} |
| Total Return (closed) | {result.total_return_pct:.2f}% |
| Max Drawdown | {result.max_drawdown:.2f}% |
| Sharpe | {result.sharpe_ratio:.2f} |
| Final Capital | ${result.final_capital:.2f} |

## Trade Analysis

| Metric | Value |
|--------|-------|
| Closed Trades | {len(closes)} |
| Winners | {len(winners)} |
| Losers | {len(losers)} |
| Avg Win | ${avg_win:.2f} |
| Avg Loss | ${avg_loss:.2f} |
| Open Positions | {len(entries)} |
| Unrealized PnL | ${open_pnl:.2f} |
| TOTAL Return | {total_return:.2f}% |

## Close Reasons

"""
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        report += f"- {reason}: {count}\n"

    output_file.write_text(report)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())