"""Pairs Trading Portfolio + Multi-Timeframe Validation.

Combines the best pairs into a portfolio and tests on daily, 4h, 1h, 15m bars.

Usage:
    uv run python scripts/backtest/pairs_portfolio_backtest.py
"""

from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, ".")

from scripts.backtest.pairs_trading_backtest import (
    PairConfig, PairResult, Trade, backtest_pair, SYMBOL_SOURCES,
    compute_spread, rolling_zscore, CAPITAL, COMMISSION, DAYS,
)

# ---------------------------------------------------------------------------
# Portfolio config
# ---------------------------------------------------------------------------

PORTFOLIO = [
    # pair_a,     pair_b,     lookback, entry_z, exit_z, stop_z, max_hold, label
    ("SHIBUSDT", "UNIUSDT",  30, 2.5, 0.0, 4.0, 20, "SHIB/UNI"),
    ("LINKUSDT", "SOLUSDT",  20, 2.5, 0.0, 4.0, 20, "LINK/SOL"),
    ("HYPEUSDT", "TRXUSDT",  20, 3.0, 0.0, 4.0, 20, "HYPE/TRX"),
]


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_bars(symbol: str, exchange: str, days: int, bar_seconds: int) -> list[tuple[int, float]]:
    """Load resampled close prices from 5m candles."""
    conn = sqlite3.connect("data/candles.db")
    cursor = conn.cursor()
    cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
    cursor.execute(
        "SELECT timestamp, close FROM candles "
        "WHERE symbol=? AND exchange=? AND timeframe='5m' AND timestamp >= ? "
        "ORDER BY timestamp ASC",
        (symbol, exchange, cutoff),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return []

    bar = {}
    for ts, close in rows:
        bar_ts = (ts // bar_seconds) * bar_seconds
        bar[bar_ts] = close

    return sorted(bar.items())


BAR_SECONDS = {
    "daily": 86400,
    "4h": 4 * 3600,
    "1h": 3600,
    "15m": 900,
}


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def compute_stats(equity_curve: list[float], bars_per_year: int) -> dict:
    """Compute return, Sharpe, Sortino, MaxDD from equity curve."""
    if len(equity_curve) < 2:
        return {"return": 0, "monthly": 0, "sharpe": 0, "sortino": 0, "max_dd": 0}

    total_ret = (equity_curve[-1] / equity_curve[0] - 1) * 100
    num_bars = len(equity_curve)
    monthly = total_ret / (num_bars / (bars_per_year / 12))

    # Max DD
    peak = equity_curve[0]
    max_dd = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Bar returns for Sharpe/Sortino
    rets = [equity_curve[i] / equity_curve[i-1] - 1 for i in range(1, len(equity_curve))]
    if not rets:
        return {"return": total_ret, "monthly": monthly, "sharpe": 0, "sortino": 0, "max_dd": max_dd}

    avg = sum(rets) / len(rets)
    var = sum((r - avg)**2 for r in rets) / len(rets)
    std = math.sqrt(var) if var > 0 else 0
    sharpe = avg / std * math.sqrt(bars_per_year) if std > 0 else 0

    downside = [r for r in rets if r < 0]
    if downside:
        ds_var = sum(r**2 for r in downside) / len(downside)
        ds_std = math.sqrt(ds_var)
        sortino = avg / ds_std * math.sqrt(bars_per_year) if ds_std > 0 else 0
    else:
        sortino = sharpe * 2

    return {
        "return": total_ret,
        "monthly": monthly,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd": max_dd,
    }


# ---------------------------------------------------------------------------
# Portfolio backtest
# ---------------------------------------------------------------------------

def backtest_portfolio(timeframe: str = "daily", days: int = 730) -> None:
    """Run portfolio backtest at given timeframe."""

    bar_sec = BAR_SECONDS[timeframe]
    bars_per_year = 365 * 86400 // bar_sec

    # Adjust lookback/max_hold for shorter timeframes
    # Daily: lookback=20 means 20 days. 4h: lookback=20*6=120 bars = 20 days equivalent
    # But we want to test the same *calendar time* parameters
    lookback_mult = bars_per_year / 365  # bars per day
    max_hold_mult = lookback_mult

    print(f"\n{'='*90}")
    print(f"  TIMEFRAME: {timeframe} ({bars_per_year} bars/year)")
    print(f"{'='*90}")

    # Load data
    symbols = set()
    for a, b, *_ in PORTFOLIO:
        symbols.add(a)
        symbols.add(b)

    price_data = {}
    for sym in symbols:
        exchange = SYMBOL_SOURCES[sym]
        data = load_bars(sym, exchange, days, bar_sec)
        if data:
            price_data[sym] = data
            d0 = datetime.fromtimestamp(data[0][0], tz=timezone.utc).strftime("%Y-%m-%d")
            d1 = datetime.fromtimestamp(data[-1][0], tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            print(f"  WARNING: No data for {sym}")

    # Run each pair
    pair_results = []
    pair_equities = []

    for a, b, lb, ez, xz, sz, mh, label in PORTFOLIO:
        if a not in price_data or b not in price_data:
            continue

        # Scale lookback and max_hold to bar count
        lb_bars = max(10, int(lb * lookback_mult))
        mh_bars = max(10, int(mh * max_hold_mult))

        cfg = PairConfig(
            pair=(a, b),
            lookback=lb_bars,
            entry_z=ez,
            exit_z=xz,
            stop_z=sz,
            max_hold=mh_bars,
            position_pct=1.0 / len(PORTFOLIO),
            label=label,
        )

        result = backtest_pair(cfg, price_data[a], price_data[b])
        pair_results.append(result)

        if result.equity_curve:
            pair_equities.append(result.equity_curve)
        else:
            pair_equities.append([])

    # Print individual pair results
    print(f"\n  {'Pair':12s} │ {'Return':>8s} {'Monthly':>8s} {'Trades':>7s} {'WR%':>6s} │ {'MaxDD':>7s} {'Sharpe':>7s} {'Sort':>6s}")
    print(f"  {'-'*75}")

    for r in pair_results:
        if not r.trades:
            print(f"  {r.config.label:12s} │ NO TRADES")
            continue
        wr = sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) * 100
        print(
            f"  {r.config.label:12s} │ {r.total_return_pct:>+7.1f}% {r.monthly_return_pct:>+7.2f}% {len(r.trades):>7d} {wr:>5.1f}% │"
            f" {r.max_dd_pct:>6.1f}% {r.sharpe:>+6.2f} {r.sortino:>+5.2f}"
        )

    # Combine equity curves into portfolio
    if pair_equities:
        min_len = min(len(eq) for eq in pair_equities if eq)
        if min_len > 0:
            combined = []
            for i in range(min_len):
                total = sum(eq[i] for eq in pair_equities if len(eq) > i)
                combined.append(total)

            stats = compute_stats(combined, bars_per_year)
            total_trades = sum(len(r.trades) for r in pair_results)
            total_comm = sum(
                sum(t.commission_pct for t in r.trades) for r in pair_results
            )

            print(f"\n  {'PORTFOLIO':12s} │ {stats['return']:>+7.1f}% {stats['monthly']:>+7.2f}% {total_trades:>7d}        │"
                  f" {stats['max_dd']:>6.1f}% {stats['sharpe']:>+6.2f} {stats['sortino']:>+5.2f}")
            print(f"  {'Commission':12s} │ {total_comm:.2f}% total")

            # Walk-forward: split in half
            mid = min_len // 2
            first_half = combined[:mid]
            second_half = combined[mid:]
            s1 = compute_stats(first_half, bars_per_year)
            s2 = compute_stats(second_half, bars_per_year)
            print(f"\n  Walk-forward validation:")
            print(f"    1st half: {s1['return']:>+7.1f}%  Sharpe {s1['sharpe']:>+5.2f}  DD {s1['max_dd']:.1f}%")
            print(f"    2nd half: {s2['return']:>+7.1f}%  Sharpe {s2['sharpe']:>+5.2f}  DD {s2['max_dd']:.1f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    timeframes = ["daily", "4h", "1h", "15m"]
    if "--tf" in sys.argv:
        idx = sys.argv.index("--tf")
        timeframes = [sys.argv[idx + 1]] if idx + 1 < len(sys.argv) else timeframes

    print(f"=== Pairs Trading Portfolio Backtest ({DAYS} days, ${CAPITAL:.0f}) ===")
    print(f"Portfolio: {', '.join(p[-1] for p in PORTFOLIO)}")
    print(f"Timeframes: {', '.join(timeframes)}")

    for tf in timeframes:
        backtest_portfolio(timeframe=tf, days=DAYS)

    print(f"\n{'='*90}")
    print("DONE")


if __name__ == "__main__":
    main()
