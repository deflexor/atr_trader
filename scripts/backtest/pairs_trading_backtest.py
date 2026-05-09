"""Pairs Trading / Statistical Arbitrage Backtest.

Strategy: Find correlated asset pairs. When the spread (log price ratio) deviates
from its rolling mean by >N standard deviations, bet on mean reversion:
  - Spread too high → short asset A, long asset B (expect spread to fall)
  - Spread too low → long asset A, short asset B (expect spread to rise)
  - Exit when spread returns to mean, or stop loss, or max holding period

This is market-neutral: directional market moves cancel out.

Usage:
    uv run python scripts/backtest/pairs_trading_backtest.py
    uv run python scripts/backtest/pairs_trading_backtest.py --quick
"""

from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DAYS = 730
COMMISSION = 0.00036  # 0.036% maker per side (Bybit USDT perp, non-VIP)
CAPITAL = 1000.0

# All symbols available (new + legacy)
# Prefer Binance for longer history, Bybit for delisted coins
SYMBOL_SOURCES = {
    "BTCUSDT": "binance", "ETHUSDT": "binance", "SOLUSDT": "binance",
    "DOGEUSDT": "binance", "ADAUSDT": "binance", "AVAXUSDT": "binance",
    "TRXUSDT": "binance", "UNIUSDT": "binance",
    "XRPUSDT": "binance", "LTCUSDT": "binance", "LINKUSDT": "binance",
    "ZECUSDT": "binance", "XMRUSDT": "bybit", "HYPEUSDT": "bybit",
    "BNBUSDT": "binance", "TONUSDT": "binance", "COMPUSDT": "binance",
    "XLMUSDT": "binance", "BCHUSDT": "binance", "SUIUSDT": "binance",
    "SHIBUSDT": "binance", "MNTUSDT": "bybit", "PAXGUSDT": "binance",
}

# Resample to daily bars for pairs trading (low frequency = low commission)
RESAMPLE = "daily"


# ---------------------------------------------------------------------------
# Pairs Config
# ---------------------------------------------------------------------------

@dataclass
class PairConfig:
    """Configuration for a single pair backtest."""
    pair: tuple[str, str]       # (symbol_a, symbol_b)
    lookback: int = 20          # Rolling window for z-score (in daily bars)
    entry_z: float = 2.0        # Z-score threshold to enter
    exit_z: float = 0.5         # Z-score threshold to exit (take profit)
    stop_z: float = 4.0         # Z-score stop loss
    max_hold: int = 20          # Max holding period (daily bars)
    position_pct: float = 0.5   # Fraction of capital per leg (0.5 = $500 each side)
    label: str = ""


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_daily_closes(symbol: str, exchange: str, days: int) -> list[tuple[int, float]]:
    """Load daily close prices from 5m candles in candles.db.

    Returns: list of (timestamp_unix_seconds, close_price), one per day.
    """
    conn = sqlite3.connect("data/candles.db")
    cursor = conn.cursor()

    cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
    cursor.execute(
        """SELECT timestamp, close FROM candles
           WHERE symbol=? AND exchange=? AND timeframe='5m' AND timestamp >= ?
           ORDER BY timestamp ASC""",
        (symbol, exchange, cutoff),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return []

    # Resample to daily: take the last 5m candle of each UTC day
    daily = {}
    for ts, close in rows:
        day_ts = (ts // 86400) * 86400  # Floor to UTC day
        daily[day_ts] = close

    return sorted(daily.items())


def load_4h_closes(symbol: str, exchange: str, days: int) -> list[tuple[int, float]]:
    """Load 4h close prices from 5m candles."""
    conn = sqlite3.connect("data/candles.db")
    cursor = conn.cursor()

    cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
    cursor.execute(
        """SELECT timestamp, close FROM candles
           WHERE symbol=? AND exchange=? AND timeframe='5m' AND timestamp >= ?
           ORDER BY timestamp ASC""",
        (symbol, exchange, cutoff),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return []

    # Resample to 4h
    bar = {}
    for ts, close in rows:
        bar_ts = (ts // (4 * 3600)) * (4 * 3600)
        bar[bar_ts] = close

    return sorted(bar.items())


# ---------------------------------------------------------------------------
# Pairs Trading Engine
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    entry_day: int
    exit_day: int
    direction: str        # "long_A_short_B" or "short_A_long_B"
    entry_z: float
    exit_z: float
    entry_spread: float
    exit_spread: float
    pnl_pct: float        # Net PnL as % of allocated capital
    commission_pct: float


@dataclass
class PairResult:
    config: PairConfig
    trades: list[Trade]
    total_return_pct: float
    monthly_return_pct: float
    max_dd_pct: float
    sharpe: float
    sortino: float
    correlation: float
    num_days: int
    equity_curve: list[float] = field(default_factory=list)


def compute_spread(prices_a: list[float], prices_b: list[float]) -> list[float]:
    """Compute log price ratio spread."""
    return [math.log(a / b) for a, b in zip(prices_a, prices_b)]


def rolling_zscore(series: list[float], lookback: int) -> list[Optional[float]]:
    """Compute rolling z-score. Returns None for initial window."""
    result = [None] * len(series)
    for i in range(lookback, len(series)):
        window = series[i - lookback:i]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std = math.sqrt(variance) if variance > 0 else 0
        result[i] = (series[i] - mean) / std if std > 1e-10 else 0.0
    return result


def backtest_pair(
    config: PairConfig,
    dates_a: list[tuple[int, float]],
    dates_b: list[tuple[int, float]],
    bar_label: str = "days",
) -> PairResult:
    """Run pairs trading backtest for a single pair."""

    # Align timestamps
    ts_map_a = dict(dates_a)
    ts_map_b = dict(dates_b)
    common_ts = sorted(set(ts_map_a.keys()) & set(ts_map_b.keys()))

    if len(common_ts) < config.lookback + 20:
        return PairResult(
            config=config, trades=[], total_return_pct=0, monthly_return_pct=0,
            max_dd_pct=0, sharpe=0, sortino=0, correlation=0, num_days=0,
        )

    prices_a = [ts_map_a[ts] for ts in common_ts]
    prices_b = [ts_map_b[ts] for ts in common_ts]

    # Correlation of returns
    rets_a = [prices_a[i+1]/prices_a[i] - 1 for i in range(len(prices_a)-1)]
    rets_b = [prices_b[i+1]/prices_b[i] - 1 for i in range(len(prices_b)-1)]
    mean_a = sum(rets_a) / len(rets_a)
    mean_b = sum(rets_b) / len(rets_b)
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(rets_a, rets_b)) / len(rets_a)
    std_a = math.sqrt(sum((a - mean_a)**2 for a in rets_a) / len(rets_a))
    std_b = math.sqrt(sum((b - mean_b)**2 for b in rets_b) / len(rets_b))
    correlation = cov / (std_a * std_b) if std_a > 0 and std_b > 0 else 0

    # Compute spread and z-scores
    spread = compute_spread(prices_a, prices_b)
    zscores = rolling_zscore(spread, config.lookback)

    # Trading simulation
    trades: list[Trade] = []
    equity = CAPITAL * config.position_pct * 2  # Capital allocated to this pair
    equity_curve = [equity]
    in_position = False
    pos_direction = ""
    entry_idx = 0
    entry_z = 0.0
    entry_spread = 0.0
    entry_price_a = 0.0
    entry_price_b = 0.0

    for i in range(config.lookback, len(common_ts)):
        z = zscores[i]
        if z is None:
            equity_curve.append(equity_curve[-1])
            continue

        if in_position:
            # Check exit conditions
            should_exit = False
            exit_reason = ""

            # Take profit: spread reverted
            if pos_direction == "long_A_short_B" and z <= config.exit_z:
                should_exit = True
                exit_reason = "tp"
            elif pos_direction == "short_A_long_B" and z >= -config.exit_z:
                should_exit = True
                exit_reason = "tp"

            # Stop loss: spread diverged further
            if pos_direction == "long_A_short_B" and z >= config.stop_z:
                should_exit = True
                exit_reason = "stop"
            elif pos_direction == "short_A_long_B" and z <= -config.stop_z:
                should_exit = True
                exit_reason = "stop"

            # Max hold
            if i - entry_idx >= config.max_hold:
                should_exit = True
                exit_reason = "maxhold"

            if should_exit:
                # Compute PnL
                exit_price_a = prices_a[i]
                exit_price_b = prices_b[i]
                exit_spread = spread[i]

                if pos_direction == "long_A_short_B":
                    # Long A: (exit - entry) / entry, Short B: (entry - exit) / entry
                    pnl_a = (exit_price_a - entry_price_a) / entry_price_a
                    pnl_b = (entry_price_b - exit_price_b) / entry_price_b
                else:
                    pnl_a = (entry_price_a - exit_price_a) / entry_price_a
                    pnl_b = (exit_price_b - exit_price_b) / entry_price_b

                gross_pnl = (pnl_a + pnl_b) / 2  # Average of both legs
                comm = COMMISSION * 4  # 4 sides: entry+exit for 2 legs

                net_pnl = gross_pnl - comm
                equity *= (1 + net_pnl)

                trades.append(Trade(
                    entry_day=entry_idx, exit_day=i,
                    direction=pos_direction,
                    entry_z=entry_z, exit_z=z,
                    entry_spread=entry_spread, exit_spread=exit_spread,
                    pnl_pct=net_pnl * 100,
                    commission_pct=comm * 100,
                ))

                in_position = False

        if not in_position:
            # Check entry
            if z >= config.entry_z:
                in_position = True
                pos_direction = "short_A_long_B"  # Spread too high, expect it to fall
                entry_idx = i
                entry_z = z
                entry_spread = spread[i]
                entry_price_a = prices_a[i]
                entry_price_b = prices_b[i]
            elif z <= -config.entry_z:
                in_position = True
                pos_direction = "long_A_short_B"  # Spread too low, expect it to rise
                entry_idx = i
                entry_z = z
                entry_spread = spread[i]
                entry_price_a = prices_a[i]
                entry_price_b = prices_b[i]

        equity_curve.append(equity)

    # Compute stats
    num_days = len(common_ts)
    total_return = (equity_curve[-1] / equity_curve[0] - 1) * 100 if equity_curve[0] > 0 else 0
    monthly_return = total_return / (num_days / 30) if num_days > 0 else 0

    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Daily returns for Sharpe/Sortino
    daily_rets = []
    for j in range(1, len(equity_curve)):
        r = equity_curve[j] / equity_curve[j-1] - 1
        daily_rets.append(r)

    sharpe = 0.0
    sortino = 0.0
    if daily_rets:
        avg_r = sum(daily_rets) / len(daily_rets)
        var_r = sum((r - avg_r)**2 for r in daily_rets) / len(daily_rets)
        std_r = math.sqrt(var_r) if var_r > 0 else 0
        if std_r > 0:
            sharpe = avg_r / std_r * math.sqrt(365)

        downside = [r for r in daily_rets if r < 0]
        if downside:
            ds_var = sum(r**2 for r in downside) / len(downside)
            ds_std = math.sqrt(ds_var)
            if ds_std > 0:
                sortino = avg_r / ds_std * math.sqrt(365)

    return PairResult(
        config=config, trades=trades,
        total_return_pct=total_return,
        monthly_return_pct=monthly_return,
        max_dd_pct=max_dd,
        sharpe=sharpe, sortino=sortino,
        correlation=correlation,
        num_days=num_days,
        equity_curve=equity_curve,
    )


# ---------------------------------------------------------------------------
# Pair Selection — test all combinations
# ---------------------------------------------------------------------------

def generate_all_pairs() -> list[tuple[str, str]]:
    """Generate all unique pairs from available symbols."""
    symbols = sorted(SYMBOL_SOURCES.keys())
    pairs = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            pairs.append((symbols[i], symbols[j]))
    return pairs


def generate_config_grid(pairs: list[tuple[str, str]], quick: bool = False) -> list[PairConfig]:
    """Generate configurations for the grid search."""
    configs = []

    if quick:
        # Test only top correlated pairs with default params
        for pair in pairs:
            configs.append(PairConfig(
                pair=pair, lookback=20, entry_z=2.0, exit_z=0.5,
                stop_z=4.0, max_hold=20, position_pct=0.5,
                label=f"{pair[0]}/{pair[1]}",
            ))
        return configs

    # Full grid: vary entry_z, lookback, exit_z
    for pair in pairs:
        for lookback in [10, 20, 30]:
            for entry_z in [1.5, 2.0, 2.5]:
                for exit_z in [0.0, 0.5]:
                    label = f"{pair[0]}/{pair[1]}_L{lookback}_E{entry_z}_X{exit_z}"
                    configs.append(PairConfig(
                        pair=pair, lookback=lookback, entry_z=entry_z, exit_z=exit_z,
                        stop_z=4.0, max_hold=20, position_pct=0.5, label=label,
                    ))
    return configs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    quick = "--quick" in sys.argv
    bar_mode = "4h" if "--4h" in sys.argv else "daily"

    print(f"=== Pairs Trading Backtest ({bar_mode} bars, {DAYS} days, ${CAPITAL:.0f}) ===")
    print()

    # Load all price data
    print(f"Loading {bar_mode} price data for {len(SYMBOL_SOURCES)} symbols...")
    price_data: dict[str, list[tuple[int, float]]] = {}
    for sym, exchange in SYMBOL_SOURCES.items():
        if bar_mode == "4h":
            data = load_4h_closes(sym, exchange, DAYS)
        else:
            data = load_daily_closes(sym, exchange, DAYS)
        if data:
            price_data[sym] = data
            d0 = datetime.fromtimestamp(data[0][0], tz=timezone.utc).strftime("%Y-%m-%d")
            d1 = datetime.fromtimestamp(data[-1][0], tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"  {sym:12s} ({exchange:8s}): {len(data):>4d} bars  {d0} → {d1}")
        else:
            print(f"  {sym:12s}: NO DATA")

    print()

    # First pass: compute correlations for all pairs to prioritize
    print("Computing pair correlations...")
    all_pairs = generate_all_pairs()
    # Only keep pairs where both symbols have data
    valid_pairs = [(a, b) for a, b in all_pairs if a in price_data and b in price_data]
    print(f"  {len(valid_pairs)} valid pairs out of {len(all_pairs)}")

    pair_corrs: dict[tuple[str, str], float] = {}
    for a, b in valid_pairs:
        # Quick correlation from aligned daily closes
        ts_a = dict(price_data[a])
        ts_b = dict(price_data[b])
        common = sorted(set(ts_a.keys()) & set(ts_b.keys()))
        if len(common) < 30:
            continue
        pa = [ts_a[t] for t in common]
        pb = [ts_b[t] for t in common]
        ra = [pa[i+1]/pa[i] - 1 for i in range(len(pa)-1)]
        rb = [pb[i+1]/pb[i] - 1 for i in range(len(pb)-1)]
        ma = sum(ra)/len(ra)
        mb = sum(rb)/len(rb)
        cov = sum((a-ma)*(b-mb) for a, b in zip(ra, rb)) / len(ra)
        sa = math.sqrt(sum((a-ma)**2 for a in ra) / len(ra))
        sb = math.sqrt(sum((b-mb)**2 for b in rb) / len(rb))
        corr = cov / (sa * sb) if sa > 0 and sb > 0 else 0
        pair_corrs[(a, b)] = corr

    # Sort by correlation (highest first) — most correlated pairs are best candidates
    sorted_pairs = sorted(pair_corrs.items(), key=lambda x: abs(x[1]), reverse=True)

    # Show top correlations
    print(f"\nTop 20 most correlated pairs:")
    for (a, b), corr in sorted_pairs[:20]:
        print(f"  {a:12s} / {b:12s}  corr = {corr:+.3f}")

    # Filter to pairs with |correlation| > 0.3
    if quick:
        # In quick mode, test top 30 correlated pairs
        test_pairs = [(a, b) for (a, b), _ in sorted_pairs[:30]]
    else:
        test_pairs = [(a, b) for (a, b), corr in sorted_pairs if abs(corr) >= 0.3]

    print(f"\nTesting {len(test_pairs)} pairs...")

    # Generate configs
    configs = generate_config_grid(test_pairs, quick=quick)
    print(f"  {len(configs)} configurations to test")
    print()

    # Run backtests
    results: list[PairResult] = []
    for i, cfg in enumerate(configs):
        a, b = cfg.pair
        if a not in price_data or b not in price_data:
            continue

        result = backtest_pair(cfg, price_data[a], price_data[b], bar_label=bar_mode)
        results.append(result)

        if (i + 1) % 100 == 0 or (i + 1) == len(configs):
            print(f"  Progress: {i+1}/{len(configs)}")

    # Sort by Sharpe ratio
    results.sort(key=lambda r: r.sharpe, reverse=True)

    # Print results
    print()
    print("=" * 120)
    print("PAIRS TRADING BACKTEST RESULTS (top 30 by Sharpe)")
    print("=" * 120)
    print(f"{'Pair':<28s} {'Lookback':>8s} {'EntryZ':>7s} {'ExitZ':>6s} │ {'Return':>8s} {'Monthly':>8s} {'Trades':>7s} {'WR%':>6s} │ {'MaxDD':>7s} {'Sharpe':>7s} {'Sort':>6s} │ {'Corr':>6s}")
    print("-" * 120)

    for r in results[:30]:
        if not r.trades:
            continue
        cfg = r.config
        wr = sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) * 100
        print(
            f"{cfg.label:<28s} {cfg.lookback:>8d} {cfg.entry_z:>7.1f} {cfg.exit_z:>6.1f} │"
            f" {r.total_return_pct:>+7.1f}% {r.monthly_return_pct:>+7.2f}% {len(r.trades):>7d} {wr:>5.1f}% │"
            f" {r.max_dd_pct:>6.1f}% {r.sharpe:>+6.2f} {r.sortino:>+5.2f} │ {r.correlation:>+5.2f}"
        )

    # Best pair detail
    if results and results[0].trades:
        best = results[0]
        cfg = best.config
        print()
        print("=" * 80)
        print("BEST PAIR DETAIL")
        print("=" * 80)
        print(f"  Pair: {cfg.pair[0]} / {cfg.pair[1]}")
        print(f"  Config: lookback={cfg.lookback}, entry_z={cfg.entry_z}, exit_z={cfg.exit_z}, stop_z={cfg.stop_z}")
        print(f"  Return: {best.total_return_pct:+.1f}% ({best.num_days} days)")
        print(f"  Monthly: {best.monthly_return_pct:+.2f}%")
        print(f"  Trades: {len(best.trades)}")
        wr = sum(1 for t in best.trades if t.pnl_pct > 0) / len(best.trades) * 100
        avg_win = sum(t.pnl_pct for t in best.trades if t.pnl_pct > 0) / max(1, sum(1 for t in best.trades if t.pnl_pct > 0))
        avg_loss = sum(t.pnl_pct for t in best.trades if t.pnl_pct <= 0) / max(1, sum(1 for t in best.trades if t.pnl_pct <= 0))
        print(f"  Win Rate: {wr:.1f}%")
        print(f"  Avg Win: +{avg_win:.2f}%, Avg Loss: {avg_loss:.2f}%")
        print(f"  Max DD: {best.max_dd_pct:.1f}%")
        print(f"  Sharpe: {best.sharpe:+.2f}, Sortino: {best.sortino:+.2f}")
        print(f"  Correlation: {best.correlation:+.3f}")
        total_comm = sum(t.commission_pct for t in best.trades)
        print(f"  Total Commission: {total_comm:.2f}%")

        # Exit reasons
        exit_reasons = {"tp": 0, "stop": 0, "maxhold": 0}
        for t in best.trades:
            # Determine exit reason from z-score behavior
            if abs(t.exit_z) <= cfg.exit_z:
                exit_reasons["tp"] += 1
            elif abs(t.exit_z) >= cfg.stop_z:
                exit_reasons["stop"] += 1
            else:
                exit_reasons["maxhold"] += 1
        print(f"  Exit reasons: {exit_reasons}")

    # Print bottom 10 (worst) for reference
    print()
    print("=" * 120)
    print("WORST 10 PAIRS (by Sharpe)")
    print("=" * 120)
    for r in results[-10:]:
        if not r.trades:
            continue
        cfg = r.config
        wr = sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) * 100
        print(
            f"{cfg.label:<28s} {r.total_return_pct:>+7.1f}% {r.monthly_return_pct:>+7.2f}% {len(r.trades):>7d} {wr:>5.1f}% │"
            f" {r.max_dd_pct:>6.1f}% {r.sharpe:>+6.2f} {r.correlation:>+5.2f}"
        )


if __name__ == "__main__":
    main()
