"""Debug: understanding the drawdown bug.

The 7-day backtest shows 7.76% max drawdown (correct).
The 30-day backtest shows 226.03% max drawdown (incorrect - should be ~2.26%).

Evidence:
- Manual calculation shows max drawdown of $226.03
- That's 2.26% of $10,000 peak equity
- But backtest reports 226.03%

This means the code is storing dollar amount but treating it as percentage!
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model


async def main():
    symbol = "BTCUSDT"
    days = 30

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
    print("KEY METRICS COMPARISON")
    print(f"{'='*60}")
    print(f"Initial Capital:  ${result.initial_capital:.2f}")
    print(f"Final Capital:   ${result.final_capital:.2f}")
    print(f"Total Return:     {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:     {result.max_drawdown:.2f}%")
    print(f"Sharpe Ratio:    {result.sharpe_ratio:.2f}")

    # Equity analysis
    equity_values = [eq["equity"] for eq in result.equity_curve]
    peak = max(equity_values)
    trough = min(equity_values)
    actual_dd_pct = (peak - trough) / peak * 100

    print(f"\n{'='*60}")
    print("EQUITY ANALYSIS")
    print(f"{'='*60}")
    print(f"Peak equity:     ${peak:.2f}")
    print(f"Trough equity:   ${trough:.2f}")
    print(f"Actual DD ($):   ${peak - trough:.2f}")
    print(f"Actual DD (%):   {actual_dd_pct:.2f}%")
    print(f"Reported DD:     {result.max_drawdown:.2f}%")

    print(f"\n{'='*60}")
    print("BUG IDENTIFIED")
    print(f"{'='*60}")
    print(f"The code stores max_drawdown as DOLLARS but reports as PERCENTAGE!")
    print(f"Peak: ${peak:.2f}, Trough: ${trough:.2f}")
    print(f"Max drawdown = ${peak - trough:.2f} (stored value)")
    print(f"But reported as {result.max_drawdown:.2f}% (treating $ as %)")
    print(f"Actual percentage should be: {actual_dd_pct:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())