"""Cointegration Screening + Daily + 1h Pairs Backtest.

Strategy:
  1. Load ALL symbols from candles.db, resample to daily + 1h
  2. Cointegration screen every pair (ADF + Engle-Granger)
  3. Backtest cointegrated pairs on BOTH daily and 1h bars
  4. Grid search: lookback [10,20,30] × entry_z [2.0,2.5,3.0] × exit_z [0.0,0.5]
  5. Walk-forward validation (H1/H2 split)
  6. Report top pairs by timeframe

Usage:
    uv run python scripts/backtest/cointegration_screener.py
    uv run python scripts/backtest/cointegration_screener.py --daily-only
    uv run python scripts/backtest/cointegration_screener.py --1h-only
"""

from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from statsmodels.tsa.stattools import adfuller, coint

sys.path.insert(0, ".")

from scripts.backtest.pairs_trading_backtest import (
    PairConfig, Trade, PairResult, backtest_pair,
    compute_spread, rolling_zscore,
    COMMISSION, CAPITAL, DAYS,
)

# ---------------------------------------------------------------------------
# Symbol universe — all symbols in DB with >= 600 days of data
# ---------------------------------------------------------------------------

# We'll dynamically query the DB, but here's the expected set
# Exclude XAUTUSDT (only 44 days), keep TONUSDT (~630 days)
ALL_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "TRXUSDT", "UNIUSDT",
    "XRPUSDT", "LTCUSDT", "LINKUSDT", "ZECUSDT", "XMRUSDT", "HYPEUSDT",
    "BNBUSDT", "TONUSDT", "COMPUSDT", "XLMUSDT", "BCHUSDT",
    "SUIUSDT", "SHIBUSDT", "MNTUSDT", "PAXGUSDT",
]
EXCHANGE_MAP = {
    "BTCUSDT": "binance", "ETHUSDT": "binance", "SOLUSDT": "binance",
    "DOGEUSDT": "binance", "ADAUSDT": "binance", "AVAXUSDT": "binance",
    "TRXUSDT": "binance", "UNIUSDT": "binance",
    "XRPUSDT": "binance", "LTCUSDT": "binance", "LINKUSDT": "binance",
    "ZECUSDT": "binance", "XMRUSDT": "bybit", "HYPEUSDT": "bybit",
    "BNBUSDT": "binance", "TONUSDT": "binance", "COMPUSDT": "binance",
    "XLMUSDT": "binance", "BCHUSDT": "binance", "SUIUSDT": "binance",
    "SHIBUSDT": "binance", "MNTUSDT": "bybit", "PAXGUSDT": "binance",
}

# Bar settings
BAR_SECONDS = {
    "daily": 86400,
    "1h": 3600,
}

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_bars(symbol: str, exchange: str, bar_seconds: int, days: int) -> list[tuple[int, float]]:
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


# ---------------------------------------------------------------------------
# Cointegration tests
# ---------------------------------------------------------------------------

def adf_test(series: list[float]) -> dict:
    arr = np.array(series)
    if np.std(arr) < 1e-10:
        return {"pvalue": 1.0, "statistic": 0.0, "is_stationary": False}
    try:
        result = adfuller(arr, maxlag=20, autolag="AIC")
        return {"pvalue": result[1], "statistic": result[0], "is_stationary": result[1] < 0.05}
    except Exception:
        return {"pvalue": 1.0, "statistic": 0.0, "is_stationary": False}


def cointegration_test(prices_a: list[float], prices_b: list[float]) -> dict:
    try:
        score, pvalue, _ = coint(prices_a, prices_b, maxlag=20, autolag="AIC")
        return {"pvalue": pvalue, "statistic": score, "is_cointegrated": pvalue < 0.05}
    except Exception:
        return {"pvalue": 1.0, "statistic": 0.0, "is_cointegrated": False}


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def compute_stats(equity_curve: list[float], bars_per_year: int) -> dict:
    if len(equity_curve) < 2:
        return {"return": 0, "monthly": 0, "sharpe": 0, "sortino": 0, "max_dd": 0, "trades": 0, "wr": 0}
    total_ret = (equity_curve[-1] / equity_curve[0] - 1) * 100
    num_bars = len(equity_curve)
    monthly = total_ret / (num_bars / (bars_per_year / 12)) if num_bars > 0 else 0
    peak = equity_curve[0]
    max_dd = 0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd: max_dd = dd
    rets = [equity_curve[i]/equity_curve[i-1]-1 for i in range(1, len(equity_curve))]
    if not rets:
        return {"return": total_ret, "monthly": monthly, "sharpe": 0, "sortino": 0, "max_dd": max_dd, "trades": 0, "wr": 0}
    avg = sum(rets)/len(rets)
    var = sum((r-avg)**2 for r in rets)/len(rets)
    std = math.sqrt(var) if var > 0 else 0
    sharpe = avg/std*math.sqrt(bars_per_year) if std > 0 else 0
    ds = [r for r in rets if r < 0]
    sortino = avg/math.sqrt(sum(r**2 for r in ds)/len(ds))*math.sqrt(bars_per_year) if ds else sharpe*2
    return {"return": total_ret, "monthly": monthly, "sharpe": sharpe, "sortino": sortino, "max_dd": max_dd}


@dataclass
class CointegrationScreen:
    pair_a: str
    pair_b: str
    label: str
    correlation: float
    adf_pvalue: float
    coint_pvalue: float
    is_cointegrated: bool
    half_life: float
    num_bars: int


def estimate_half_life(spread: list[float]) -> float:
    if len(spread) < 20:
        return float("inf")
    arr = np.array(spread)
    ds = np.diff(arr)
    lagged = arr[:-1]
    try:
        lam = np.sum(lagged * ds) / np.sum(lagged ** 2)
    except Exception:
        return float("inf")
    if lam >= 0:
        return float("inf")
    hl = -math.log(2) / lam
    return max(1.0, hl)


def screen_all_pairs(
    price_data: dict[str, list[tuple[int, float]]],
    exclude_pairs: set | None = None,
) -> list[CointegrationScreen]:
    """Run cointegration screening on ALL symbol pairs."""
    symbols = sorted(price_data.keys())
    results = []

    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            a, b = symbols[i], symbols[j]
            if exclude_pairs and ((a, b) in exclude_pairs or (b, a) in exclude_pairs):
                continue

            ts_a = dict(price_data[a])
            ts_b = dict(price_data[b])
            common = sorted(set(ts_a.keys()) & set(ts_b.keys()))
            if len(common) < 100:
                continue

            pa = [ts_a[t] for t in common]
            pb = [ts_b[t] for t in common]

            # Correlation
            ra = np.diff(pa) / np.array(pa[:-1])
            rb = np.diff(pb) / np.array(pb[:-1])
            corr = float(np.corrcoef(ra, rb)[0, 1]) if len(ra) > 1 else 0

            # Spread
            spread = [math.log(pa[i]/pb[i]) for i in range(len(pa))]

            # Cointegration
            adf_r = adf_test(spread)
            coin_r = cointegration_test(pa, pb)

            # Half-life
            hl = estimate_half_life(spread)

            # Cointegrated if BOTH pass (we require spread stationary AND prices cointegrated)
            coint = adf_r["pvalue"] < 0.05 and coin_r["pvalue"] < 0.05

            label = f"{a.replace('USDT','')}/{b.replace('USDT','')}"
            results.append(CointegrationScreen(
                pair_a=a, pair_b=b, label=label,
                correlation=corr,
                adf_pvalue=adf_r["pvalue"],
                coint_pvalue=coin_r["pvalue"],
                is_cointegrated=coint,
                half_life=hl,
                num_bars=len(common),
            ))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    daily_only = "--daily-only" in sys.argv
    h1_only = "--1h-only" in sys.argv

    if daily_only:
        timeframes = ["daily"]
    elif h1_only:
        timeframes = ["1h"]
    else:
        timeframes = ["daily", "1h"]

    print(f"=== Cointegration Screening + Pairs Backtest ===")
    print(f"Universe: {len(ALL_SYMBOLS)} symbols, {DAYS}d history, comm={COMMISSION*100:.3f}%")
    print(f"Timeframes: {timeframes}")
    print(f"Bar types: {COMMISSION*100:.3f}% = 0.036% maker per side × 4 sides/round-trip")
    print()

    # -----------------------------------------------------------------------
    # Load data for ALL symbols
    # -----------------------------------------------------------------------
    price_data: dict[str, dict[str, list[tuple[int, float]]]] = {}
    # price_data[symbol][timeframe] = [(ts, close), ...]

    for sym in ALL_SYMBOLS:
        exchange = EXCHANGE_MAP.get(sym, "binance")
        price_data[sym] = {}
        for tf in timeframes:
            data = load_bars(sym, exchange, BAR_SECONDS[tf], DAYS)
            if data:
                price_data[sym][tf] = data

    # Filter to symbols with data
    valid_symbols = [s for s in ALL_SYMBOLS if "daily" in price_data.get(s, {}) and price_data[s]["daily"]]
    print(f"Symbols with data: {len(valid_symbols)}/{len(ALL_SYMBOLS)}")
    for s in valid_symbols:
        d = price_data[s]["daily"]
        d0 = datetime.fromtimestamp(d[0][0], tz=timezone.utc).strftime("%Y-%m-%d")
        d1 = datetime.fromtimestamp(d[-1][0], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  {s:12s}  {len(d):>5d} daily bars  {d0} → {d1}")
    print()

    # -----------------------------------------------------------------------
    # STEP 1: Cointegration Screening (on daily bars)
    # -----------------------------------------------------------------------
    print("=" * 100)
    print("STEP 1: COINTEGRATION SCREENING (daily bars)")
    print("=" * 100)

    daily_data = {s: price_data[s]["daily"] for s in valid_symbols if "daily" in price_data[s]}
    screens = screen_all_pairs(daily_data)

    # Sort: cointegrated first, then by correlation
    screens.sort(key=lambda s: (not s.is_cointegrated, -s.correlation))

    # Count
    n_pairs = len(screens)
    n_coint = sum(1 for s in screens if s.is_cointegrated)
    n_adf = sum(1 for s in screens if s.adf_pvalue < 0.05)
    n_coin = sum(1 for s in screens if s.coint_pvalue < 0.05)
    print(f"\nTotal pairs tested: {n_pairs}")
    print(f"ADF stationary (p<0.05): {n_adf}")
    print(f"Engle-Granger cointegrated (p<0.05): {n_coin}")
    print(f"Both (ADF + Coint): {n_coint}")
    print()

    # Print ALL cointegrated pairs
    print("COINTEGRATED PAIRS (ADF p<0.05 AND Coint p<0.05):")
    print(f"  {'Pair':<18s} {'Corr':>7s} {'ADF p':>8s} {'Coint p':>9s} {'HalfLife':>9s} {'Bars':>6s}")
    print(f"  {'-'*65}")
    for s in screens:
        if s.is_cointegrated:
            hl_str = f"{s.half_life:.0f}d" if s.half_life < float("inf") else "∞"
            print(f"  {s.label:<18s} {s.correlation:>+6.3f} {s.adf_pvalue:>8.3f} {s.coint_pvalue:>9.3f} {hl_str:>9s} {s.num_bars:>6d}")

    if n_coint == 0:
        print("  ⚠  NO PAIRS PASSED COINTEGRATION. Falling back to ADF p<0.10 or Coint p<0.10")
        # Relax threshold
        for s in screens:
            s.is_cointegrated = s.adf_pvalue < 0.10 or s.coint_pvalue < 0.10
        n_coint = sum(1 for s in screens if s.is_cointegrated)
        print(f"  {n_coint} pairs with relaxed threshold")
    print()

    # Print top 30 by correlation (for reference)
    print("TOP PAIRS BY CORRELATION (for reference):")
    print(f"  {'Pair':<18s} {'Corr':>7s} {'ADF p':>8s} {'Coint p':>9s} {'Coint?':>7s} {'HL':>6s}")
    print(f"  {'-'*65}")
    for s in screens[:30]:
        hl_str = f"{s.half_life:.0f}" if s.half_life < float("inf") else "∞"
        coint_flag = "✓" if s.is_cointegrated else ""
        print(f"  {s.label:<18s} {s.correlation:>+6.3f} {s.adf_pvalue:>8.3f} {s.coint_pvalue:>9.3f} {coint_flag:>7s} {hl_str:>6s}")

    # -----------------------------------------------------------------------
    # STEP 2: Backtest cointegrated pairs on EACH timeframe
    # -----------------------------------------------------------------------
    for tf in timeframes:
        tf_data = {s: price_data[s][tf] for s in valid_symbols if tf in price_data[s]}
        bar_sec = BAR_SECONDS[tf]
        bars_per_year = 365 * 86400 // bar_sec

        # Scale lookback: daily 20 → 1h 20*24=480
        lookback_mult = bars_per_year / 365

        print()
        print("=" * 120)
        print(f"STEP 2: BACKTEST — {tf.upper()} BARS ({bars_per_year} bars/year)")
        print("=" * 120)

        # Generate grid configs for cointegrated pairs
        coint_pairs_screens = [s for s in screens if s.is_cointegrated]
        configs: list[PairConfig] = []

        for screen in coint_pairs_screens:
            if screen.pair_a not in tf_data or screen.pair_b not in tf_data:
                continue
            for lookback in [10, 20, 30]:
                lb_bars = max(10, int(lookback * lookback_mult))
                for entry_z in [2.0, 2.5, 3.0]:
                    for exit_z in [0.0, 0.5]:
                        label = f"{screen.label}_L{lookback}E{entry_z}X{exit_z}"
                        configs.append(PairConfig(
                            pair=(screen.pair_a, screen.pair_b),
                            lookback=lb_bars,
                            entry_z=entry_z,
                            exit_z=exit_z,
                            stop_z=4.0,
                            max_hold=max(20, int(30 * lookback_mult)),
                            position_pct=0.5,
                            label=label,
                        ))

        if not configs:
            print(f"  No cointegrated pairs to test on {tf}")
            continue

        print(f"  Testing {len(configs)} configs across {len(set(c.pair for c in configs))} pairs...\n")

        # Run all configs
        results: list[PairResult] = []
        for i, cfg in enumerate(configs):
            a, b = cfg.pair
            if a not in tf_data or b not in tf_data:
                continue
            result = backtest_pair(cfg, tf_data[a], tf_data[b], bar_label=tf)
            results.append(result)
            if (i + 1) % 50 == 0:
                print(f"    Progress: {i+1}/{len(configs)}")

        # Sort by Sharpe
        results.sort(key=lambda r: r.sharpe, reverse=True)

        # Print top results
        print(f"\n  TOP 20 RESULTS (by Sharpe):")
        print(f"  {'Config':<32s} │ {'Return':>8s} {'Monthly':>8s} {'Trades':>7s} {'WR%':>6s} │ {'MaxDD':>7s} {'Sharpe':>7s} {'Sort':>6s} │ {'Corr':>6s}")
        print(f"  {'-'*110}")

        for r in results[:20]:
            if not r.trades:
                continue
            cfg = r.config
            wr = sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) * 100
            print(
                f"  {cfg.label:<32s} │ {r.total_return_pct:>+7.1f}% {r.monthly_return_pct:>+7.2f}% {len(r.trades):>7d} {wr:>5.1f}% │"
                f" {r.max_dd_pct:>6.1f}% {r.sharpe:>+6.2f} {r.sortino:>+5.2f} │ {r.correlation:>+5.2f}"
            )

        # -------------------------------------------------------------------
        # STEP 3: Walk-forward validation on best configs
        # -------------------------------------------------------------------
        print()
        print(f"  WALK-FORWARD VALIDATION (best config per pair, {tf}):")
        print()

        seen_pairs = set()
        best_per_pair = []
        for r in results:
            if r.config.pair not in seen_pairs and r.trades:
                seen_pairs.add(r.config.pair)
                best_per_pair.append(r)
            if len(best_per_pair) >= 6:
                break

        print(f"  {'Config':<32s} │ {'Full':>25s} │ {'1st Half':>15s} │ {'2nd Half':>15s}")
        print(f"  {'':32s} │ {'Return  Sharpe  Tr':>25s} │ {'Ret  Sharpe':>15s} │ {'Ret  Sharpe':>15s}")
        print(f"  {'-'*115}")

        for r in best_per_pair:
            if not r.equity_curve or len(r.equity_curve) < 10:
                continue
            cfg = r.config
            a, b = cfg.pair

            # Split data for walk-forward
            ts_a = dict(tf_data[a])
            ts_b = dict(tf_data[b])
            common = sorted(set(ts_a.keys()) & set(ts_b.keys()))
            mid = len(common) // 2

            dates_a_full = [(t, p) for t, p in tf_data[a] if t in set(common)]
            dates_b_full = [(t, p) for t, p in tf_data[b] if t in set(common)]
            da1 = [(t, p) for t, p in dates_a_full if t <= common[mid]]
            da2 = [(t, p) for t, p in dates_a_full if t > common[mid]]
            db1 = [(t, p) for t, p in dates_b_full if t <= common[mid]]
            db2 = [(t, p) for t, p in dates_b_full if t > common[mid]]

            r1 = backtest_pair(cfg, da1, db1, bar_label=f"{tf}_h1")
            r2 = backtest_pair(cfg, da2, db2, bar_label=f"{tf}_h2")

            both = "✓" if r1.sharpe > 0 and r2.sharpe > 0 else "✗"
            both2 = "✓✓" if r1.total_return_pct > 0 and r2.total_return_pct > 0 else "✗✗"

            print(f"  {cfg.label:<32s} │ {r.total_return_pct:>+7.1f}%  {r.sharpe:>+5.2f}  {len(r.trades):>4d}  │ {r1.total_return_pct:>+6.1f}%  {r1.sharpe:>+5.2f}  │ {r2.total_return_pct:>+6.1f}%  {r2.sharpe:>+5.2f}  {both2}")

    # -----------------------------------------------------------------------
    # CROSS-TIMEFRAME SUMMARY
    # -----------------------------------------------------------------------
    if len(timeframes) > 1:
        print()
        print("=" * 100)
        print("CROSS-TIMEFRAME COMPARISON")
        print("=" * 100)
        print()
        print("Key question: Does 1h edge survive commission drag?")
        print(f"  Commission per round-trip: {COMMISSION*4*100:.3f}% (0.036% × 4 sides)")
        print(f"  Daily: ~365 bars/year → low trade frequency → commission manageable")
        print(f"  1h: ~8760 bars/year → high trade frequency → commission can dominate")
        print(f"  If 1h trades significantly more but returns similar → commission eats edge")

    print()
    print("=== DONE ===")


if __name__ == "__main__":
    main()
