"""Deep debug 30-day backtest to find why drawdown is so high."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.WARNING, format="%(message)s")


async def main():
    symbol = "BTCUSDT"
    days = 30

    print(f"Fetching {days} days of data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)
    print(f"Got {len(raw)} candles raw")

    # Parse candles
    candles_list = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles_list.append(Candle(
                symbol=symbol, exchange="bybit", timeframe="1m",
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

    candles = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1m")
    print(f"Using {len(candles.candles)} 1m candles")

    # Load H1Model
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    # Test WITHOUT Kelly (baseline 30-day)
    config = MomentumConfig(
        name="baseline_30day",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        trailing_activation_atr=3.0,
        trailing_distance_atr=2.5,
        max_drawdown_pct=0.0,  # No halt for debug
        use_trailing_stop=True,
    )
    engine = BacktestEngine(engine_config)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    print("\nRunning 30-day backtest (no Kelly)...")
    result = await engine.run(candles, signal_gen, 10000.0)

    print(f"\n{'='*60}")
    print("30-DAY RESULTS (no Kelly)")
    print(f"{'='*60}")
    print(f"Total Trades:     {result.total_trades}")
    print(f"Win Rate:         {result.win_rate:.1%}")
    print(f"Return:           {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:     {result.max_drawdown:.2f}%")
    print(f"Final Capital:    ${result.final_capital:.2f}")

    # Analyze trade PnLs
    print(f"\n{'='*60}")
    print("TRADE PNL ANALYSIS")
    print(f"{'='*60}")
    pnls = [t.get("pnl", 0) for t in result.trades if t.get("pnl") is not None]
    if pnls:
        print(f"Winners: {sum(1 for p in pnls if p > 0)} ({sum(1 for p in pnls if p > 0)/len(pnls)*100:.1f}%)")
        print(f"Losers:  {sum(1 for p in pnls if p <= 0)} ({sum(1 for p in pnls if p <= 0)/len(pnls)*100:.1f}%)")
        print(f"Avg PnL: ${sum(pnls)/len(pnls):.4f}")
        print(f"Total PnL: ${sum(pnls):.4f}")

        winning = [p for p in pnls if p > 0]
        losing = [p for p in pnls if p <= 0]
        if winning:
            print(f"Avg Win: ${sum(winning)/len(winning):.4f}")
        if losing:
            print(f"Avg Loss: ${sum(losing)/len(losing):.4f}")

    # Show all trades
    print(f"\n{'='*60}")
    print("ALL TRADES")
    print(f"{'='*60}")
    for i, trade in enumerate(result.trades):
        pnl = trade.get("pnl", 0)
        side = trade.get("side", "?")
        entry = trade.get("entry_price", 0)
        exit = trade.get("exit_price", 0)
        reason = trade.get("reason", "?")
        ts = trade.get("timestamp", "")
        print(f"  {i+1:2}: {side:5} entry=${entry:.2f} exit=${exit:.2f} pnl=${pnl:+.4f} [{reason}]")

    # Equity curve analysis
    print(f"\n{'='*60}")
    print("EQUITY CURVE ANALYSIS")
    print(f"{'='*60}")
    equity_values = [eq["equity"] for eq in result.equity_curve]
    print(f"Starting equity: ${equity_values[0]:.2f}")
    print(f"Min equity:     ${min(equity_values):.2f}")
    print(f"Max equity:      ${max(equity_values):.2f}")
    print(f"Ending equity:  ${equity_values[-1]:.2f}")

    # Find when equity peaked and troughed
    peak_idx = equity_values.index(max(equity_values))
    trough_idx = equity_values.index(min(equity_values))
    print(f"Peak at index:  {peak_idx} (candle {peak_idx})")
    print(f"Trough at index: {trough_idx} (candle {trough_idx})")

    # Show equity around trough
    start = max(0, trough_idx - 10)
    end = min(len(equity_values), trough_idx + 10)
    print(f"\nEquity around trough ({trough_idx}):")
    for i in range(start, end):
        marker = " <-- TROUGH" if i == trough_idx else ""
        marker += " <-- PEAK" if equity_values[i] == max(equity_values) and i >= trough_idx else ""
        print(f"  {i}: ${equity_values[i]:.2f}{marker}")


if __name__ == "__main__":
    asyncio.run(main())