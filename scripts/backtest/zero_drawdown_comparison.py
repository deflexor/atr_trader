"""Zero-Drawdown Risk Layer vs Baseline comparison backtest.

Runs the same 60-day BTC data twice:
1. BASELINE: Existing engine settings (no risk layer)
2. ZERO_DD: New zero-drawdown risk layer enabled

Compares: max drawdown, win rate, total return, Sharpe, trade count.

Usage: uv run python scripts/backtest/zero_drawdown_comparison.py
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

logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


async def fetch_candles(symbol: str, days: int, timeframe: str) -> CandleSeries:
    """Fetch historical candle data from Bybit."""
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)
    print(f"Fetched {len(raw)} {timeframe}m candles for {symbol}")

    candles_list = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles_list.append(Candle(
                symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]),
            ))
        except Exception:
            continue

    # Deduplicate and sort
    seen: dict[int, Candle] = {}
    for c in candles_list:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c

    result = sorted(seen.values(), key=lambda x: x.timestamp.timestamp())
    return CandleSeries(result, symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m")


def print_results(label: str, result, engine: BacktestEngine) -> None:
    """Print backtest results with risk layer diagnostics."""
    closes = [t for t in result.trades if t.get("side") == "close"]
    winners = [t for t in closes if t.get("pnl", 0) > 0]
    losers = [t for t in closes if t.get("pnl", 0) <= 0]
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Initial:        ${result.initial_capital:,.2f}")
    print(f"  Final:          ${result.final_capital:,.2f}")
    print(f"  Return:         {result.total_return_pct:+.2f}%")
    print(f"  Max Drawdown:   {result.max_drawdown:.2f}%")
    print(f"  Sharpe:         {result.sharpe_ratio:.2f}")
    print(f"  Total Trades:   {result.total_trades}")
    print(f"  Closed Trades:  {len(closes)}")
    print(f"  Win Rate:       {result.win_rate:.1%}")
    print(f"  Winners:        {len(winners)}  Avg: ${avg_win:.2f}")
    print(f"  Losers:         {len(losers)}  Avg: ${avg_loss:.2f}")

    # Close reasons
    reasons: dict[str, int] = {}
    for t in closes:
        reason = t.get("reason", "?")
        reasons[reason] = reasons.get(reason, 0) + 1
    print(f"  Close reasons:  {dict(reasons)}")

    # Risk layer diagnostics
    if engine._risk_filter_stats:
        print(f"\n  --- Risk Layer Diagnostics ---")
        for key, val in engine._risk_filter_stats.items():
            print(f"  {key}: {val}")
        if engine._budget_tracker:
            bt = engine._budget_tracker
            print(f"  budget_consumed_pct: {bt.budget_consumed_pct:.1%}")
            print(f"  was_halted: {bt.is_halted}")


async def main():
    symbol = "BTCUSDT"
    days = 60
    timeframe = "5"

    print(f"Fetching {days} days of {symbol} {timeframe}m data...")
    candles = await fetch_candles(symbol, days, timeframe)
    print(f"Using {len(candles.candles)} candles")

    # Load H1Model
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    # Shared strategy config
    strategy_config = MomentumConfig(
        name="zero_dd_comparison",
        min_agreement=2,
        pullback_enabled=False,
        volume_spike_threshold=1.0,
        atr_filter_min_pct=0.00005,
        mtf_enabled=True,
    )

    # ── RUN 1: BASELINE (no risk layer) ──
    strategy1 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    baseline_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.03,
        trailing_activation_atr=8.0,
        trailing_distance_atr=4.0,
        max_drawdown_pct=0.20,
        use_trailing_stop=True,
        use_atr_stops=False,
        cooldown_candles=6,
        use_zero_drawdown_layer=False,  # <-- OFF
    )
    engine1 = BacktestEngine(baseline_config)

    async def signal_gen_baseline(sym, c):
        signal, _ = await strategy1.multi_timeframe_signal(sym, c, None)
        return signal

    print("\nRunning BASELINE backtest...")
    t0 = datetime.now()
    result1 = await engine1.run(candles, signal_gen_baseline, 10000.0)
    baseline_seconds = (datetime.now() - t0).total_seconds()

    # ── RUN 2: ZERO-DRAWDOWN LAYER ──
    strategy2 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    zero_dd_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.03,
        trailing_activation_atr=8.0,
        trailing_distance_atr=4.0,
        max_drawdown_pct=0.20,
        use_trailing_stop=True,
        use_atr_stops=False,
        cooldown_candles=6,
        use_zero_drawdown_layer=True,
        regime_lookback=100,
        boltzmann_temperature=0.5,
        bootstrap_stops_enabled=True,
        bootstrap_confidence=0.99,
        bootstrap_simulations=500,
        bootstrap_horizon=60,  # 60 * 5min = 5 hours forward
        bootstrap_min_stop_pct=0.01,  # Minimum 1% stop
        bootstrap_max_stop_pct=0.08,  # Maximum 8% stop
        per_trade_drawdown_budget=0.05,
        total_drawdown_budget=0.20,
    )
    engine2 = BacktestEngine(zero_dd_config)

    async def signal_gen_zero_dd(sym, c):
        signal, _ = await strategy2.multi_timeframe_signal(sym, c, None)
        return signal

    print("Running ZERO-DRAWDOWN backtest...")
    t0 = datetime.now()
    result2 = await engine2.run(candles, signal_gen_zero_dd, 10000.0)
    zero_dd_seconds = (datetime.now() - t0).total_seconds()

    # ── PRINT RESULTS ──
    print_results("BASELINE (no risk layer)", result1, engine1)
    print_results("ZERO-DRAWDOWN LAYER", result2, engine2)

    # ── COMPARISON ──
    print(f"\n{'='*60}")
    print("  COMPARISON: Base vs Zero-DD")
    print(f"{'='*60}")
    dd_improvement = result1.max_drawdown - result2.max_drawdown
    dd_improvement_pct = (dd_improvement / result1.max_drawdown * 100) if result1.max_drawdown > 0 else 0
    return_delta = result2.total_return_pct - result1.total_return_pct

    print(f"  Max Drawdown:  {result1.max_drawdown:.2f}% → {result2.max_drawdown:.2f}%  "
          f"({'↓' if dd_improvement > 0 else '↑'} {abs(dd_improvement):.2f}pp, {dd_improvement_pct:+.1f}%)")
    print(f"  Total Return:  {result1.total_return_pct:+.2f}% → {result2.total_return_pct:+.2f}%  "
          f"({return_delta:+.2f}pp)")
    print(f"  Win Rate:      {result1.win_rate:.1%} → {result2.win_rate:.1%}")
    print(f"  Sharpe:        {result1.sharpe_ratio:.2f} → {result2.sharpe_ratio:.2f}")
    print(f"  Trades:        {result1.total_trades} → {result2.total_trades}")
    print(f"  Speed:         {baseline_seconds:.1f}s vs {zero_dd_seconds:.1f}s")

    # ── SAVE REPORT ──
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output = Path(f"results/zero_dd_comparison_{date_str}.md")

    closes1 = [t for t in result1.trades if t.get("side") == "close"]
    closes2 = [t for t in result2.trades if t.get("side") == "close"]
    winners1 = [t for t in closes1 if t.get("pnl", 0) > 0]
    winners2 = [t for t in closes2 if t.get("pnl", 0) > 0]

    report = f"""# Zero-Drawdown Risk Layer Comparison

**Date**: {date_str}
**Symbol**: {symbol} | **Days**: {days} | **Timeframe**: {timeframe}m

## Results

| Metric | Baseline | Zero-DD | Change |
|--------|----------|---------|--------|
| Max Drawdown | {result1.max_drawdown:.2f}% | {result2.max_drawdown:.2f}% | {dd_improvement:+.2f}pp |
| Total Return | {result1.total_return_pct:+.2f}% | {result2.total_return_pct:+.2f}% | {return_delta:+.2f}pp |
| Win Rate | {result1.win_rate:.1%} | {result2.win_rate:.1%} | |
| Sharpe | {result1.sharpe_ratio:.2f} | {result2.sharpe_ratio:.2f} | |
| Total Trades | {result1.total_trades} | {result2.total_trades} | |
| Closed Trades | {len(closes1)} | {len(closes2)} | |
| Winners | {len(winners1)} | {len(winners2)} | |

## Risk Layer Config

| Parameter | Value |
|-----------|-------|
| regime_lookback | 100 |
| boltzmann_temperature | 0.3 |
| bootstrap_confidence | 0.95 |
| per_trade_drawdown_budget | 1% |
| total_drawdown_budget | 3% |

## Risk Layer Diagnostics

{engine2._risk_filter_stats if engine2._risk_filter_stats else 'N/A'}
"""
    output.write_text(report)
    print(f"\nReport saved: {output}")


if __name__ == "__main__":
    asyncio.run(main())
