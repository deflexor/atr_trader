"""Trailing Stop Optimization Script

Tests different trailing stop ATR distance combinations on 7-day backtest.
Runs 16 combinations (4 activation x 4 distance) and outputs comparison table.

Usage: uv run python scripts/backtest/trailing_stop_optimization.py
"""

import asyncio
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# Test combinations: (activation_atr, distance_atr)
TRAILING_CONFIGS = [
    (1.5, 1.5),
    (1.5, 2.0),
    (1.5, 2.5),
    (1.5, 3.0),
    (2.0, 1.5),
    (2.0, 2.0),
    (2.0, 2.5),
    (2.0, 3.0),
    (2.5, 1.5),
    (2.5, 2.0),
    (2.5, 2.5),
    (2.5, 3.0),
    (3.0, 1.5),
    (3.0, 2.0),
    (3.0, 2.5),
    (3.0, 3.0),
]


def run_backtest(
    candles: CandleSeries,
    signal_generator: Callable,
    config: BacktestConfig,
    initial_capital: float = 10000.0,
) -> tuple[BacktestConfig, dict]:
    """Run single backtest and return config + metrics."""
    engine = BacktestEngine(config)
    result = asyncio.get_event_loop().run_until_complete(
        engine.run(candles, signal_generator, initial_capital)
    )
    return config, {
        "activation_atr": config.trailing_activation_atr,
        "distance_atr": config.trailing_distance_atr,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "max_drawdown": result.max_drawdown,
        "sharpe_ratio": result.sharpe_ratio,
        "total_return_pct": result.total_return_pct,
        "avg_win": result.avg_win,
        "avg_loss": result.avg_loss,
    }


async def fetch_candles(symbol: str, days: int) -> CandleSeries:
    """Fetch 1m candles from Bybit."""
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)

    candles = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles.append(Candle(
                symbol=symbol,
                exchange="bybit",
                timeframe="1m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            ))
        except Exception:
            continue

    candles.sort(key=lambda x: x.timestamp.timestamp())
    seen = {}
    result = []
    for c in candles:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)

    return CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1m")


def print_comparison_table(results: list[dict]) -> None:
    """Print formatted comparison table."""
    header = f"{'Activation':>10} {'Distance':>10} {'Trades':>8} {'Win%':>8} {'MaxDD%':>8} {'Sharpe':>8}"
    print("\n" + "=" * 70)
    print("TRAILING STOP OPTIMIZATION RESULTS")
    print("=" * 70)
    print(header)
    print("-" * 70)

    for r in sorted(results, key=lambda x: x["sharpe_ratio"], reverse=True):
        row = (
            f"{r['activation_atr']:>10.1f} "
            f"{r['distance_atr']:>10.1f} "
            f"{r['total_trades']:>8} "
            f"{r['win_rate']*100:>7.1f}% "
            f"{r['max_drawdown']:>7.2f}% "
            f"{r['sharpe_ratio']:>8.2f}"
        )
        print(row)

    print("-" * 70)
    # Best by Sharpe
    best = max(results, key=lambda x: x["sharpe_ratio"])
    print(f"\nBest ATR Config: activation={best['activation_atr']}, distance={best['distance_atr']}")
    print(f"  Sharpe: {best['sharpe_ratio']:.2f} | Trades: {best['total_trades']} | WinRate: {best['win_rate']*100:.1f}%")


def save_results_md(results: list[dict], date_str: str) -> Path:
    """Save results to markdown file."""
    filepath = Path(f"results/trailing_stop_optimization_{date_str}.md")
    filepath.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Trailing Stop Optimization Results",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat()}",
        "\n## Configuration",
        "- Symbol: BTCUSDT",
        "- Backtest Period: 7 days",
        "- Initial Capital: 10,000 USDT",
        "\n## Results (sorted by Sharpe ratio desc)",
        "\n| Activation ATR | Distance ATR | Trades | Win Rate | Max DD | Sharpe |",
        "|---|---|---|---|---|---|---|",
    ]

    for r in sorted(results, key=lambda x: x["sharpe_ratio"], reverse=True):
        lines.append(
            f"| {r['activation_atr']:.1f} | {r['distance_atr']:.1f} | "
            f"{r['total_trades']} | {r['win_rate']*100:.1f}% | "
            f"{r['max_drawdown']:.2f}% | {r['sharpe_ratio']:.2f} |"
        )

    best = max(results, key=lambda x: x["sharpe_ratio"])
    lines.extend([
        "\n## Best Configuration",
        f"- **Activation ATR**: {best['activation_atr']}",
        f"- **Distance ATR**: {best['distance_atr']}",
        f"- **Sharpe Ratio**: {best['sharpe_ratio']:.2f}",
        f"- **Total Trades**: {best['total_trades']}",
        f"- **Win Rate**: {best['win_rate']*100:.1f}%",
        f"- **Max Drawdown**: {best['max_drawdown']:.2f}%",
        f"- **Return**: {best['total_return_pct']:.2f}%",
    ])

    filepath.write_text("\n".join(lines))
    return filepath


async def main():
    symbol = "BTCUSDT"
    days = 7
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 1. Fetch candles
    print(f"Fetching {days} days of 1m candles from Bybit...")
    candles_1m = await fetch_candles(symbol, days)
    print(f"Got {len(candles_1m.candles)} 1m candles")

    if len(candles_1m.candles) < 1000:
        print(f"WARNING: Only {len(candles_1m.candles)} candles - may be insufficient")

    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")
    else:
        print("WARNING: H1Model not found - using momentum strategy only")

    # 3. Setup strategy
    config_h1 = MomentumConfig(
        name="h1_test",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config_h1, h1_model=h1_model)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    # 4. Run backtest for each trailing config
    print("\nRunning trailing stop optimization (16 combinations)...")
    results = []

    for activation, distance in TRAILING_CONFIGS:
        config = BacktestConfig(
            initial_capital=10000.0,
            use_trailing_stop=True,
            trailing_activation_atr=activation,
            trailing_distance_atr=distance,
            use_atr_stops=False,  # Disable fixed ATR stops, use trailing only
            atr_sl_multiplier=99.0,
            atr_tp_multiplier=99.0,
        )

        print(f"  Testing activation={activation}, distance={distance}...", end=" ", flush=True)
        engine = BacktestEngine(config)
        result = await engine.run(candles_1m, signal_gen, 10000.0)

        metrics = {
            "activation_atr": activation,
            "distance_atr": distance,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "max_drawdown": result.max_drawdown,
            "sharpe_ratio": result.sharpe_ratio,
            "total_return_pct": result.total_return_pct,
            "avg_win": result.avg_win,
            "avg_loss": result.avg_loss,
        }
        results.append(metrics)
        print(f"trades={result.total_trades}, sharpe={result.sharpe_ratio:.2f}")

    # 5. Print comparison table
    print_comparison_table(results)

    # 6. Save results
    filepath = save_results_md(results, date_str)
    print(f"\nResults saved to: {filepath}")


if __name__ == "__main__":
    asyncio.run(main())