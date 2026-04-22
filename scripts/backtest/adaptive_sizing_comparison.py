"""Phase 2 adaptive sizing comparison: Zero-DD (Phase 1) vs Zero-DD + Adaptive (Phase 2).

Uses local candles.db to avoid API timeout.
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
        name="adaptive_comparison",
        min_agreement=2,
        pullback_enabled=False,
        volume_spike_threshold=1.0,
        atr_filter_min_pct=0.00005,
        mtf_enabled=True,
    )

    # ── RUN 1: ZERO-DD PHASE 1 (no adaptive) ──
    strategy1 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    phase1_config = BacktestConfig(
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
        use_adaptive_sizing=False,  # <-- OFF
    )
    engine1 = BacktestEngine(phase1_config)

    async def gen1(sym, c):
        sig, _ = await strategy1.multi_timeframe_signal(sym, c, None)
        return sig

    print("\nRunning PHASE 1 (Zero-DD, no adaptive)...")
    result1 = await engine1.run(candles, gen1, 10000.0)

    # ── RUN 2: ZERO-DD PHASE 2 (with adaptive) ──
    strategy2 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    phase2_config = BacktestConfig(
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
        use_adaptive_sizing=True,  # <-- ON
        adaptive_thresholds=((-1.0, 0.25), (-2.0, 0.50), (-3.0, 1.0)),
        adaptive_cooldown_candles=5,
    )
    engine2 = BacktestEngine(phase2_config)

    async def gen2(sym, c):
        sig, _ = await strategy2.multi_timeframe_signal(sym, c, None)
        return sig

    print("Running PHASE 2 (Zero-DD + Adaptive Sizing)...")
    result2 = await engine2.run(candles, gen2, 10000.0)

    # ── PRINT ──
    print_results("PHASE 1: Zero-DD (no adaptive)", result1, engine1)
    print_results("PHASE 2: Zero-DD + Adaptive Sizing", result2, engine2)

    # ── COMPARISON ──
    dd_delta = result1.max_drawdown - result2.max_drawdown
    ret_delta = result2.total_return_pct - result1.total_return_pct
    print(f"\n{'='*60}")
    print("  COMPARISON: Phase 1 vs Phase 2")
    print(f"{'='*60}")
    print(f"  Max Drawdown:  {result1.max_drawdown:.2f}% → {result2.max_drawdown:.2f}%  "
          f"({'↓' if dd_delta > 0 else '↑'} {abs(dd_delta):.2f}pp)")
    print(f"  Total Return:  {result1.total_return_pct:+.2f}% → {result2.total_return_pct:+.2f}%  "
          f"({ret_delta:+.2f}pp)")
    print(f"  Win Rate:      {result1.win_rate:.1%} → {result2.win_rate:.1%}")
    print(f"  Sharpe:        {result1.sharpe_ratio:.2f} → {result2.sharpe_ratio:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
