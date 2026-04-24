"""Phase 3 velocity sizing comparison: Phase 1 vs Phase 2 Adaptive vs Phase 3 Velocity.

Compares three configurations:
  1. Phase 1: Zero-DD only (no adaptive, no velocity)
  2. Phase 2: Zero-DD + Adaptive (absolute P&L level thresholds)
  3. Phase 3: Zero-DD + Velocity (rate-of-loss thresholds)

Approach D hypothesis: velocity thresholds should trigger fewer false positives
(won't cut winners that dip then recover) while still catching rapid drawdowns.
"""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model


def load_candles_from_db(symbol: str, timeframe: str, days: int) -> CandleSeries:
    """Load candles from local SQLite DB."""
    conn = sqlite3.connect("data/candles.db")
    cur = conn.cursor()
    cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
    rows = cur.execute(
        "SELECT timestamp, open, high, low, close, volume "
        "FROM candles WHERE symbol=? AND timeframe=? AND timestamp>=? "
        "ORDER BY timestamp",
        (symbol, timeframe, cutoff),
    ).fetchall()
    conn.close()

    candles_list = []
    for ts, o, h, l, c, v in rows:
        candles_list.append(Candle(
            symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m",
            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
            open=float(o), high=float(h), low=float(l),
            close=float(c), volume=float(v),
        ))
    return CandleSeries(candles_list, symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m")


def print_results(label: str, result, engine: BacktestEngine) -> None:
    closes = [t for t in result.trades if t.get("side") == "close"]
    partials = [t for t in result.trades if t.get("side") == "partial_close"]
    winners = [t for t in closes if t.get("pnl", 0) > 0]
    losers = [t for t in closes if t.get("pnl", 0) <= 0]
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Return:         {result.total_return_pct:+.2f}%")
    print(f"  Max Drawdown:   {result.max_drawdown:.2f}%")
    print(f"  Sharpe:         {result.sharpe_ratio:.2f}")
    print(f"  Win Rate:       {result.win_rate:.1%}")
    print(f"  Total Trades:   {result.total_trades}")
    print(f"  Closed Trades:  {len(closes)}")
    print(f"  Partial Closes: {len(partials)}")
    print(f"  Winners:        {len(winners)}  Avg: ${avg_win:.2f}")
    print(f"  Losers:         {len(losers)}  Avg: ${avg_loss:.2f}")

    reasons: dict[str, int] = {}
    for t in closes:
        reason = t.get("reason", "?")
        reasons[reason] = reasons.get(reason, 0) + 1
    print(f"  Close reasons:  {dict(reasons)}")

    if partials:
        partial_pnl = sum(t["pnl"] for t in partials)
        print(f"  Partial PnL:    ${partial_pnl:.2f}")
        partial_reasons: dict[str, int] = {}
        for t in partials:
            reason = t.get("reason", "?")
            partial_reasons[reason] = partial_reasons.get(reason, 0) + 1
        print(f"  Partial reasons: {dict(partial_reasons)}")

    if engine._risk_filter_stats:
        print(f"\n  --- Risk Layer ---")
        for key, val in engine._risk_filter_stats.items():
            if val > 0:
                print(f"  {key}: {val}")


async def main():
    symbol = "BTCUSDT"
    timeframe = "5m"
    days = 60

    print(f"Loading {days} days of {symbol} {timeframe} data from local DB...")
    candles = load_candles_from_db(symbol, timeframe, days)
    print(f"Loaded {len(candles.candles)} candles")

    if len(candles.candles) < 100:
        print("ERROR: Not enough candles. Need at least 60 days of 5m data.")
        return

    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    strategy_config = MomentumConfig(
        name="velocity_comparison",
        min_agreement=2,
        pullback_enabled=False,
        volume_spike_threshold=1.0,
        atr_filter_min_pct=0.00005,
        mtf_enabled=True,
    )

    base_config = dict(
        initial_capital=10000.0,
        risk_per_trade=0.03,
        trailing_activation_atr=8.0,
        trailing_distance_atr=4.0,
        max_drawdown_pct=0.20,
        use_trailing_stop=True,
        cooldown_candles=6,
        use_zero_drawdown_layer=True,
        regime_lookback=100,
        boltzmann_temperature=0.5,
        bootstrap_stops_enabled=False,
        per_trade_drawdown_budget=0.05,
        total_drawdown_budget=0.20,
    )

    # ── RUN 1: PHASE 1 (no adaptive, no velocity) ──
    strategy1 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    cfg1 = BacktestConfig(
        **base_config,
        use_adaptive_sizing=False,
        use_velocity_sizing=False,
    )
    engine1 = BacktestEngine(cfg1)
    async def gen1(sym, c):
        sig, _ = await strategy1.multi_timeframe_signal(sym, c, None)
        return sig
    print("\nRunning PHASE 1 (Zero-DD only)...")
    result1 = await engine1.run(candles, gen1, 10000.0)

    # ── RUN 2: PHASE 2 (regime-aware adaptive sizing) ──
    strategy2 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    cfg2 = BacktestConfig(
        **base_config,
        use_adaptive_sizing=True,
        use_velocity_sizing=False,
    )
    engine2 = BacktestEngine(cfg2)
    async def gen2(sym, c):
        sig, _ = await strategy2.multi_timeframe_signal(sym, c, None)
        return sig
    print("Running PHASE 2 (Zero-DD + Adaptive)...")
    result2 = await engine2.run(candles, gen2, 10000.0)

    # ── RUN 3: PHASE 3 (velocity-based sizing) ──
    strategy3 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    cfg3 = BacktestConfig(
        **base_config,
        use_adaptive_sizing=False,
        use_velocity_sizing=True,
        velocity_cooldown_candles=10,
        velocity_min_energy=0.3,
        velocity_min_pnl_pct=-0.3,
        velocity_acceleration_scale=1.3,
        velocity_window_candles=5,
        velocity_min_samples=3,
    )
    engine3 = BacktestEngine(cfg3)
    async def gen3(sym, c):
        sig, _ = await strategy3.multi_timeframe_signal(sym, c, None)
        return sig
    print("Running PHASE 3 (Zero-DD + Velocity)...")
    result3 = await engine3.run(candles, gen3, 10000.0)

    # ── PRINT ──
    print_results("PHASE 1: Zero-DD (no intra-trade sizing)", result1, engine1)
    print_results("PHASE 2: Zero-DD + Adaptive (P&L level thresholds)", result2, engine2)
    print_results("PHASE 3: Zero-DD + Velocity (rate-of-loss thresholds)", result3, engine3)

    # ── COMPARISON TABLE ──
    print(f"\n{'='*70}")
    print("  COMPARISON TABLE")
    print(f"{'='*70}")
    print(f"  {'Metric':<20} {'Phase 1':>10} {'Phase 2':>10} {'Phase 3':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'Return':<20} {result1.total_return_pct:>+9.2f}% {result2.total_return_pct:>+9.2f}% {result3.total_return_pct:>+9.2f}%")
    print(f"  {'Max Drawdown':<20} {result1.max_drawdown:>9.2f}% {result2.max_drawdown:>9.2f}% {result3.max_drawdown:>9.2f}%")
    print(f"  {'Sharpe':<20} {result1.sharpe_ratio:>10.2f} {result2.sharpe_ratio:>10.2f} {result3.sharpe_ratio:>10.2f}")
    print(f"  {'Win Rate':<20} {result1.win_rate:>9.1%} {result2.win_rate:>9.1%} {result3.win_rate:>9.1%}")

    partials1 = sum(1 for t in result1.trades if t.get("side") == "partial_close")
    partials2 = sum(1 for t in result2.trades if t.get("side") == "partial_close")
    partials3 = sum(1 for t in result3.trades if t.get("side") == "partial_close")
    print(f"  {'Partial Closes':<20} {partials1:>10} {partials2:>10} {partials3:>10}")

    # ── CRASH WEEK ANALYSIS ──
    print(f"\n{'='*70}")
    print("  CRASH WEEK ANALYSIS (Feb 5-6, 2026)")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
