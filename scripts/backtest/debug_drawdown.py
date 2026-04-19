"""Debug drawdown calculation in metrics.py."""

import asyncio
from datetime import datetime, timezone

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model
from src.backtest.metrics import PerformanceMetrics

from pathlib import Path


async def main():
    symbol = "BTCUSDT"
    days = 30  # Changed from 7 to 30

    print("Fetching data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)

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

    seen = {}
    result = []
    for c in candles_list:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    result.sort(key=lambda x: x.timestamp.timestamp())

    candles = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1m")
    print(f"Using {len(candles.candles)} candles")

    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))

    config = MomentumConfig(
        name="debug",
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
        max_drawdown_pct=0.0,
        use_trailing_stop=True,
    )
    engine = BacktestEngine(engine_config)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    result = await engine.run(candles, signal_gen, 10000.0)

    print(f"\n{'='*60}")
    print("BACKTEST RESULT")
    print(f"{'='*60}")
    print(f"Initial Capital: ${result.initial_capital:.2f}")
    print(f"Final Capital:   ${result.final_capital:.2f}")
    print(f"Total Return:    {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:    {result.max_drawdown:.2f}%")

    print(f"\n{'='*60}")
    print("EQUITY CURVE (sample)")
    print(f"{'='*60}")
    equity_curve = result.equity_curve

    # Show equity around min
    equity_values = [eq["equity"] for eq in equity_curve]
    min_idx = equity_values.index(min(equity_values))
    print(f"Min equity at index {min_idx}: ${equity_values[min_idx]:.2f}")

    # Show equity around max
    max_idx = equity_values.index(max(equity_values))
    print(f"Max equity at index {max_idx}: ${equity_values[max_idx]:.2f}")

    # Manual calculation of max drawdown
    print(f"\n{'='*60}")
    print("MANUAL DRAWDOWN CALCULATION")
    print(f"{'='*60}")

    peak = equity_values[0]
    max_dd = 0
    max_dd_pct = 0
    for i, eq in enumerate(equity_values):
        if eq > peak:
            peak = eq
        dd = peak - eq
        dd_pct = dd / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
            print(f"  New max DD at index {i}: ${max_dd:.2f} ({max_dd_pct:.4f}%)")

    print(f"\nFinal manual max drawdown: ${max_dd:.2f} ({max_dd_pct:.4f}%)")
    print(f"Backtest reports: {result.max_drawdown:.2f}%")

    # Check metrics object
    print(f"\n{'='*60}")
    print("METRICS OBJECT")
    print(f"{'='*60}")
    print(f"metrics.max_drawdown: {engine.metrics.max_drawdown:.2f}%")

    # Check trade-level PnL for perspective
    print(f"\n{'='*60}")
    print("TRADE PNL VALUES")
    print(f"{'='*60}")
    for t in result.trades:
        if t.get("pnl") is not None:
            print(f"  pnl=${t['pnl']:.4f}")


if __name__ == "__main__":
    asyncio.run(main())