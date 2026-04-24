"""Phase 4 correlation monitoring comparison: Phase 1 vs Phase 3 vs Phase 4.

Compares three configurations:
  1. Phase 1: Zero-DD only (no adaptive, no velocity, no correlation)
  2. Phase 3: Zero-DD + Velocity (rate-of-loss thresholds, no correlation)
  3. Phase 4: Zero-DD + Velocity + Correlation (ETH leading BTC indicator)

Approach B hypothesis: ETH dropping before BTC is a leading indicator.
When detected, trailing stops are tightened pre-emptively, potentially
preventing drawdown before BTC actually drops.
"""

import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model


def load_candles_from_db(symbol: str, timeframe: str, days: int) -> CandleSeries | None:
    """Load candles from local SQLite DB. Returns None if no data found.
    
    Note: timeframe should be '5m' not '5' for DB queries.
    Uses the most recent `days` of data available (not from today).
    """
    try:
        conn = sqlite3.connect("data/candles.db")
        cur = conn.cursor()
        # Find the latest timestamp in DB and work backwards
        latest = cur.execute(
            "SELECT MAX(timestamp) FROM candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()[0]
        if latest is None:
            conn.close()
            return None
        cutoff = latest - days * 86400
        rows = cur.execute(
            "SELECT timestamp, open, high, low, close, volume "
            "FROM candles WHERE symbol=? AND timeframe=? AND timestamp>=? "
            "ORDER BY timestamp",
            (symbol, timeframe, cutoff),
        ).fetchall()
        conn.close()
    except Exception:
        return None

    if not rows:
        return None

    candles_list = []
    for ts, o, h, l, c, v in rows:
        candles_list.append(Candle(
            symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m",
            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
            open=float(o), high=float(h), low=float(l),
            close=float(c), volume=float(v),
        ))
    return CandleSeries(candles_list, symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m")


async def fetch_candles_from_bybit(symbol: str, timeframe: str, days: int) -> CandleSeries | None:
    """Fetch candles from Bybit API as fallback."""
    from src.adapters.bybit_adapter import BybitAdapter

    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    print(f"  Fetching {symbol} {timeframe}m from Bybit ({days} days)...")
    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)
    print(f"  Got {len(raw)} raw candles from Bybit")

    if not raw:
        return None

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

    # Deduplicate by timestamp
    seen = {}
    for c in candles_list:
        ts_key = int(c.timestamp.timestamp())
        if ts_key not in seen:
            seen[ts_key] = c
    candles_list = sorted(seen.values(), key=lambda x: x.timestamp.timestamp())

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

    # Correlation signal stats
    if engine._last_correlation_signal is not None:
        sig = engine._last_correlation_signal
        print(f"\n  --- Last Correlation Signal ---")
        print(f"  Risk level:     {sig.risk_level.value}")
        print(f"  ETH return:     {sig.eth_return_pct:+.2f}%")
        print(f"  BTC return:     {sig.btc_return_pct:+.2f}%")
        print(f"  Divergence:     {sig.divergence_pct:+.2f}%")
        if sig.trailing_atr_multiplier is not None:
            print(f"  Trailing mult:  {sig.trailing_atr_multiplier:.2f}")


async def main():
    symbol = "BTCUSDT"
    eth_symbol = "ETHUSDT"
    db_timeframe = "5m"  # DB stores as '5m', Bybit API uses '5'
    days = 30

    # Load BTC candles — try DB first, then Bybit
    print(f"Loading {days} days of {symbol} {db_timeframe} data...")
    candles = load_candles_from_db(symbol, db_timeframe, days)
    if candles and len(candles.candles) >= 100:
        print(f"Loaded {len(candles.candles)} BTC candles from DB")
    else:
        print("BTC data not in DB, fetching from Bybit...")
        candles = await fetch_candles_from_bybit(symbol, "5", days)
        if candles is None:
            print("ERROR: Could not fetch BTC data from Bybit either.")
            return
        print(f"Loaded {len(candles.candles)} BTC candles from Bybit")

    # Load ETH candles for correlation monitoring — try DB first, then Bybit
    eth_candles = None
    print(f"\nLoading {days} days of {eth_symbol} {db_timeframe} data...")
    eth_candles = load_candles_from_db(eth_symbol, db_timeframe, days)
    if eth_candles and len(eth_candles.candles) >= 100:
        print(f"Loaded {len(eth_candles.candles)} ETH candles from DB")
    else:
        print("ETH data not in DB, fetching from Bybit...")
        eth_candles = await fetch_candles_from_bybit(eth_symbol, "5", days)
        if eth_candles:
            print(f"Loaded {len(eth_candles.candles)} ETH candles from Bybit")
        else:
            print("WARNING: Could not load ETH candles. Phase 4 will run without correlation data.")

    # Align ETH candles to BTC timestamps (required for correlation monitoring)
    if eth_candles and candles:
        btc_ts_set = {int(c.timestamp.timestamp()): i for i, c in enumerate(candles.candles)}
        aligned_eth = []
        for ec in eth_candles.candles:
            ts_key = int(ec.timestamp.timestamp())
            if ts_key in btc_ts_set:
                aligned_eth.append(ec)
        if aligned_eth:
            eth_candles = CandleSeries(
                aligned_eth, symbol=eth_symbol, exchange="bybit", timeframe=db_timeframe,
            )
            print(f"Aligned ETH candles: {len(aligned_eth)} (matching BTC timestamps)")
        else:
            eth_candles = None
            print("WARNING: No matching timestamps between BTC and ETH candles")

    if len(candles.candles) < 100:
        print("ERROR: Not enough candles. Need at least 60 days of 5m data.")
        return

    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    strategy_config = MomentumConfig(
        name="correlation_comparison",
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

    # ── RUN 1: PHASE 1 (no adaptive, no velocity, no correlation) ──
    strategy1 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    cfg1 = BacktestConfig(
        **base_config,
        use_adaptive_sizing=False,
        use_velocity_sizing=False,
        use_correlation_monitor=False,
    )
    engine1 = BacktestEngine(cfg1)
    async def gen1(sym, c):
        sig, _ = await strategy1.multi_timeframe_signal(sym, c, None)
        return sig
    print("\nRunning PHASE 1 (Zero-DD only)...")
    result1 = await engine1.run(candles, gen1, 10000.0)

    # ── RUN 2: PHASE 3 (velocity sizing, no correlation) ──
    strategy2 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    cfg2 = BacktestConfig(
        **base_config,
        use_adaptive_sizing=False,
        use_velocity_sizing=True,
        use_correlation_monitor=False,
        velocity_cooldown_candles=10,
        velocity_min_energy=0.3,
        velocity_min_pnl_pct=-0.3,
        velocity_acceleration_scale=1.3,
        velocity_window_candles=5,
        velocity_min_samples=3,
    )
    engine2 = BacktestEngine(cfg2)
    async def gen2(sym, c):
        sig, _ = await strategy2.multi_timeframe_signal(sym, c, None)
        return sig
    print("Running PHASE 3 (Zero-DD + Velocity)...")
    result2 = await engine2.run(candles, gen2, 10000.0)

    # ── RUN 3: PHASE 4 (velocity + correlation monitoring) ──
    strategy3 = MomentumStrategy(config=strategy_config, h1_model=h1_model)
    cfg3 = BacktestConfig(
        **base_config,
        use_adaptive_sizing=False,
        use_velocity_sizing=True,
        use_correlation_monitor=True,
        velocity_cooldown_candles=10,
        velocity_min_energy=0.3,
        velocity_min_pnl_pct=-0.3,
        velocity_acceleration_scale=1.3,
        velocity_window_candles=5,
        velocity_min_samples=3,
        correlation_lookback_candles=20,
        correlation_mild_divergence_pct=-0.8,
        correlation_strong_divergence_pct=-1.6,
        correlation_extreme_divergence_pct=-2.5,
        correlation_trailing_elevated=0.75,
        correlation_trailing_high=0.50,
        correlation_trailing_extreme=0.25,
        correlation_reduce_at_extreme=0.25,
    )
    engine3 = BacktestEngine(cfg3)
    async def gen3(sym, c):
        sig, _ = await strategy3.multi_timeframe_signal(sym, c, None)
        return sig
    print("Running PHASE 4 (Zero-DD + Velocity + Correlation)...")
    result3 = await engine3.run(candles, gen3, 10000.0, secondary_candles=eth_candles)

    # ── PRINT ──
    print_results("PHASE 1: Zero-DD (no intra-trade sizing)", result1, engine1)
    print_results("PHASE 3: Zero-DD + Velocity (rate-of-loss thresholds)", result2, engine2)
    print_results("PHASE 4: Zero-DD + Velocity + Correlation (ETH leading BTC)", result3, engine3)

    # ── COMPARISON TABLE ──
    print(f"\n{'='*70}")
    print("  COMPARISON TABLE")
    print(f"{'='*70}")
    print(f"  {'Metric':<20} {'Phase 1':>10} {'Phase 3':>10} {'Phase 4':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'Return':<20} {result1.total_return_pct:>+9.2f}% {result2.total_return_pct:>+9.2f}% {result3.total_return_pct:>+9.2f}%")
    print(f"  {'Max Drawdown':<20} {result1.max_drawdown:>9.2f}% {result2.max_drawdown:>9.2f}% {result3.max_drawdown:>9.2f}%")
    print(f"  {'Sharpe':<20} {result1.sharpe_ratio:>10.2f} {result2.sharpe_ratio:>10.2f} {result3.sharpe_ratio:>10.2f}")
    print(f"  {'Win Rate':<20} {result1.win_rate:>9.1%} {result2.win_rate:>9.1%} {result3.win_rate:>9.1%}")

    partials1 = sum(1 for t in result1.trades if t.get("side") == "partial_close")
    partials2 = sum(1 for t in result2.trades if t.get("side") == "partial_close")
    partials3 = sum(1 for t in result3.trades if t.get("side") == "partial_close")
    print(f"  {'Partial Closes':<20} {partials1:>10} {partials2:>10} {partials3:>10}")


if __name__ == "__main__":
    asyncio.run(main())
