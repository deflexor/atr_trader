"""Threshold sweep for H1Model confidence thresholds.

Tests thresholds 0.3, 0.4, 0.5, 0.6 on 7-day backtest and compares:
- Win rate
- Total trades
- Max drawdown
- h1_model_filtered count

Usage: uv run python scripts/backtest/threshold_sweep.py
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from tabulate import tabulate

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

THRESHOLDS = [0.3, 0.4, 0.5, 0.6]


async def run_backtest_with_threshold(
    candles_1m: CandleSeries,
    h1_model: H1Model,
    threshold: float,
) -> dict:
    """Run backtest with a specific H1Model confidence threshold."""
    # Create strategy with MTF enabled
    config = MomentumConfig(
        name=f"h1_thresh_{threshold}",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    # Override threshold via environment variable hack
    # Store original and patch for this run
    original_confirm = h1_model.confirm_trend

    def patched_confirm(candles_1h, market_data=None):
        result = original_confirm(candles_1h, market_data)
        trend_agrees, direction, confidence, probs = result
        # Re-apply threshold check
        from src.ml.model import CLASS_FLAT
        new_trend_agrees = direction != CLASS_FLAT and confidence > threshold
        return (new_trend_agrees, direction, confidence, probs)

    h1_model.confirm_trend = patched_confirm

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    engine = BacktestEngine(BacktestConfig(initial_capital=10000.0))
    result = await engine.run(candles_1m, signal_gen, 10000.0)

    # Restore original
    h1_model.confirm_trend = original_confirm

    return {
        "threshold": threshold,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "max_drawdown": result.max_drawdown,
        "total_return_pct": result.total_return_pct,
        "sharpe_ratio": result.sharpe_ratio,
        "h1_model_filtered": strategy.diagnostics.get("h1_model_filtered", 0),
    }


async def main():
    symbol = "BTCUSDT"
    days = 7
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)

    # 1. Fetch 1m candles from Bybit
    print(f"Fetching {days} days of 1m candles from Bybit...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)

    candles = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles.append(Candle(
                symbol=symbol, exchange="bybit", timeframe="1m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])
            ))
        except Exception:
            continue

    candles.sort(key=lambda x: x.timestamp.timestamp())
    seen = {}
    result_list = []
    for c in candles:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result_list.append(c)

    candles_1m = CandleSeries(result_list, symbol=symbol, exchange="bybit", timeframe="1m")
    print(f"Got {len(candles_1m.candles)} 1m candles")

    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")
    else:
        print("WARNING: H1Model not found - using untrained model")

    # 3. Run threshold sweep
    print(f"\nRunning threshold sweep: {THRESHOLDS}")
    all_results = []

    for threshold in THRESHOLDS:
        print(f"  Testing threshold={threshold}...", end=" ", flush=True)
        result = await run_backtest_with_threshold(candles_1m, h1_model, threshold)
        all_results.append(result)
        print(f"trades={result['total_trades']}, win={result['win_rate']:.1%}")

    # 4. Print comparison table
    print("\n" + "=" * 70)
    print("THRESHOLD SWEEP RESULTS")
    print("=" * 70)

    table_data = [
        [
            r["threshold"],
            r["total_trades"],
            f"{r['win_rate']:.1%}",
            f"{r['max_drawdown']:.2f}%",
            f"{r['total_return_pct']:.2f}%",
            f"{r['sharpe_ratio']:.2f}",
            r["h1_model_filtered"],
        ]
        for r in all_results
    ]

    headers = ["Threshold", "Trades", "Win Rate", "Max DD", "Return %", "Sharpe", "H1 Filtered"]
    print(tabulate(table_data, headers=headers, tablefmt="grid"))

    # 5. Identify best threshold
    # Best = highest win rate with at least 5 trades
    valid_results = [r for r in all_results if r["total_trades"] >= 5]
    if valid_results:
        best = max(valid_results, key=lambda x: x["win_rate"])
        print(f"\nBest threshold: {best['threshold']} (win rate: {best['win_rate']:.1%})")
    else:
        best = all_results[0]
        print("\nNote: Not enough trades to determine best threshold")

    # 6. Save results
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_file = results_dir / f"threshold_sweep_{date_str}.md"

    report = f"""# H1Model Threshold Sweep Results

Date: {date_str}
Symbol: {symbol}
Days: {days}
Candles: {len(candles_1m.candles)}

## Thresholds Tested: {THRESHOLDS}

## Results

| Threshold | Trades | Win Rate | Max Drawdown | Return % | Sharpe | H1 Filtered |
|------------|--------|----------|--------------|----------|--------|------------|
"""
    for r in all_results:
        report += f"| {r['threshold']} | {r['total_trades']} | {r['win_rate']:.1%} | {r['max_drawdown']:.2f}% | {r['total_return_pct']:.2f}% | {r['sharpe_ratio']:.2f} | {r['h1_model_filtered']} |\n"

    report += f"""
## Best Threshold: {best['threshold']}

- Win Rate: {best['win_rate']:.1%}
- Total Trades: {best['total_trades']}
- Max Drawdown: {best['max_drawdown']:.2f}%
- Total Return: {best['total_return_pct']:.2f}%
- H1 Model Filtered: {best['h1_model_filtered']}

## Analysis

- Higher thresholds = more selective (fewer trades, potentially higher win rate)
- Lower thresholds = more signals (more trades, potentially lower win rate)
- Trade-off between signal frequency and quality
"""
    output_file.write_text(report)
    print(f"\nResults saved to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
