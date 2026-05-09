"""Enhanced Pairs Trading Backtest with ML-inspired Improvements.

Improvements over basic z-score approach:
  1. ADF cointegration test for pair selection (not just correlation)
  2. Kalman filter for dynamic hedge ratio (not equal-dollar)
  3. Ornstein-Uhlenbeck half-life for adaptive exit timing
  4. Rolling ADF regime detection (pause when cointegration breaks)

Usage:
    uv run python scripts/backtest/enhanced_pairs_backtest.py
    uv run python scripts/backtest/enhanced_pairs_backtest.py --quick
"""

from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from scipy import linalg
from statsmodels.tsa.stattools import adfuller, coint

sys.path.insert(0, ".")

from scripts.backtest.pairs_trading_backtest import (
    PairConfig, Trade, PairResult,
    load_daily_closes, compute_spread, rolling_zscore,
    SYMBOL_SOURCES, COMMISSION, CAPITAL, DAYS,
)

# ---------------------------------------------------------------------------
# 1. ADF Cointegration Test for Pair Selection
# ---------------------------------------------------------------------------

def adf_test(series: list[float]) -> dict:
    """Run Augmented Dickey-Fuller test. Returns p-value and test stat."""
    arr = np.array(series)
    if np.std(arr) < 1e-10:
        return {"pvalue": 1.0, "statistic": 0.0, "is_stationary": False}
    try:
        result = adfuller(arr, maxlag=20, autolag="AIC")
        return {
            "pvalue": result[1],
            "statistic": result[0],
            "is_stationary": result[1] < 0.05,
        }
    except Exception:
        return {"pvalue": 1.0, "statistic": 0.0, "is_stationary": False}


def cointegration_test(prices_a: list[float], prices_b: list[float]) -> dict:
    """Test cointegration between two price series using Engle-Granger."""
    try:
        score, pvalue, _ = coint(prices_a, prices_b, maxlag=20, autolag="AIC")
        return {"pvalue": pvalue, "statistic": score, "is_cointegrated": pvalue < 0.05}
    except Exception:
        return {"pvalue": 1.0, "statistic": 0.0, "is_cointegrated": False}


# ---------------------------------------------------------------------------
# 2. Kalman Filter for Dynamic Hedge Ratio
# ---------------------------------------------------------------------------

class KalmanHedge:
    """Kalman filter for estimating time-varying hedge ratio β.
    
    Model: price_A = β * price_B + ε
    State: β_t = β_{t-1} + w  (random walk)
    """

    def __init__(self, q: float = 1e-5, r: float = 1e-3):
        self.beta = 1.0     # Initial hedge ratio
        self.P = 1.0        # State covariance
        self.Q = q           # Process noise (how fast β changes)
        self.R = r           # Observation noise
        self.history: list[float] = [1.0]

    def update(self, price_a: float, price_b: float) -> float:
        """Update hedge ratio with new observation. Returns current β."""
        if abs(price_b) < 1e-10:
            self.history.append(self.beta)
            return self.beta

        # Predict
        beta_pred = self.beta
        P_pred = self.P + self.Q

        # Innovation
        y = price_a - beta_pred * price_b
        S = price_b ** 2 * P_pred + self.R

        # Kalman gain
        if abs(S) < 1e-15:
            K = 0.0
        else:
            K = P_pred * price_b / S

        # Update
        self.beta = beta_pred + K * y
        self.P = (1 - K * price_b) * P_pred
        self.beta = max(0.1, min(self.beta, 5.0))  # Clamp to reasonable range

        self.history.append(self.beta)
        return self.beta

    @property
    def current_beta(self) -> float:
        return self.beta


# ---------------------------------------------------------------------------
# 3. Ornstein-Uhlenbeck Half-Life Estimation
# ---------------------------------------------------------------------------

def estimate_half_life(spread: list[float]) -> float:
    """Estimate half-life of mean reversion via OU process.
    
    Fits: Δspread = λ * spread_{t-1} + ε
    Half-life = -ln(2) / λ
    
    Returns half-life in bars. Lower = faster reversion = better pair.
    """
    if len(spread) < 20:
        return float("inf")

    arr = np.array(spread)
    ds = np.diff(arr)
    lagged = arr[:-1]

    # OLS: ds = lambda * lagged + epsilon
    try:
        lam = np.sum(lagged * ds) / np.sum(lagged ** 2)
    except Exception:
        return float("inf")

    if lam >= 0:
        return float("inf")  # Not mean-reverting

    hl = -math.log(2) / lam
    return max(1.0, hl)


def estimate_half_life_rolling(spread: list[float], window: int) -> list[Optional[float]]:
    """Rolling half-life estimation."""
    result = [None] * len(spread)
    for i in range(window, len(spread)):
        w = spread[i - window:i]
        result[i] = estimate_half_life(w)
    return result


# ---------------------------------------------------------------------------
# 4. Rolling ADF for Regime Detection
# ---------------------------------------------------------------------------

def rolling_adf_pvalue(spread: list[float], window: int = 60) -> list[Optional[float]]:
    """Compute rolling ADF p-value. Returns None for initial window."""
    result = [None] * len(spread)
    for i in range(window, len(spread)):
        w = spread[i - window:i]
        r = adf_test(w)
        result[i] = r["pvalue"]
    return result


# ---------------------------------------------------------------------------
# Enhanced Backtest Engine
# ---------------------------------------------------------------------------

@dataclass
class EnhancedConfig:
    """Configuration for enhanced pairs trading."""
    pair: tuple[str, str]
    lookback: int = 20
    entry_z: float = 2.5
    exit_z: float = 0.0
    stop_z: float = 4.0
    max_hold_multiplier: float = 2.0  # max_hold = half_life * this
    default_max_hold: int = 30        # Fallback if half-life estimation fails
    position_pct: float = 0.5
    # Enhancement toggles
    use_kalman: bool = True
    use_halflife_exit: bool = True
    use_regime_filter: bool = True
    use_adf_selection: bool = True
    # Regime filter
    regime_adf_window: int = 60
    regime_adf_threshold: float = 0.10  # Pause if ADF p-value > this
    label: str = ""


@dataclass
class EnhancedResult:
    config: EnhancedConfig
    trades: list[Trade]
    total_return_pct: float
    monthly_return_pct: float
    max_dd_pct: float
    sharpe: float
    sortino: float
    correlation: float
    adf_pvalue: float = 1.0
    coint_pvalue: float = 1.0
    avg_half_life: float = 0.0
    avg_hedge_ratio: float = 1.0
    regime_filtered_pct: float = 0.0
    num_days: int = 0
    equity_curve: list[float] = field(default_factory=list)


def backtest_enhanced_pair(
    config: EnhancedConfig,
    dates_a: list[tuple[int, float]],
    dates_b: list[tuple[int, float]],
) -> EnhancedResult:
    """Run enhanced pairs trading backtest."""

    # Align timestamps
    ts_map_a = dict(dates_a)
    ts_map_b = dict(dates_b)
    common_ts = sorted(set(ts_map_a.keys()) & set(ts_map_b.keys()))

    if len(common_ts) < config.lookback + 30:
        return EnhancedResult(
            config=config, trades=[], total_return_pct=0, monthly_return_pct=0,
            max_dd_pct=0, sharpe=0, sortino=0, correlation=0, num_days=0,
        )

    prices_a = np.array([ts_map_a[ts] for ts in common_ts])
    prices_b = np.array([ts_map_b[ts] for ts in common_ts])

    # --- Pre-flight checks ---
    # Correlation
    rets_a = np.diff(prices_a) / prices_a[:-1]
    rets_b = np.diff(prices_b) / prices_b[:-1]
    correlation = float(np.corrcoef(rets_a, rets_b)[0, 1])

    # ADF test on initial spread
    initial_spread = compute_spread(prices_a.tolist(), prices_b.tolist())
    adf_result = adf_test(initial_spread[:60])
    coint_result = cointegration_test(prices_a.tolist(), prices_b.tolist())

    # --- Enhancement 1: Hedge ratio ---
    if config.use_kalman:
        # Initialize Kalman filter with OLS estimate
        kalman = KalmanHedge(q=1e-5, r=1e-3)
        # Warm up with first lookback points
        for i in range(min(config.lookback, len(prices_a))):
            kalman.update(prices_a[i], prices_b[i])
        # Compute dynamic spread: log(A) - beta * log(B)
        betas = []
        dynamic_spread = []
        for i in range(len(prices_a)):
            b = kalman.update(prices_a[i], prices_b[i])
            betas.append(b)
            dynamic_spread.append(math.log(prices_a[i]) - b * math.log(prices_b[i]))
        avg_beta = float(np.mean(betas[config.lookback:]))
    else:
        # Simple log ratio
        dynamic_spread = compute_spread(prices_a.tolist(), prices_b.tolist())
        betas = [1.0] * len(prices_a)
        avg_beta = 1.0

    # --- Enhancement 3: Half-life estimation ---
    half_lives = estimate_half_life_rolling(dynamic_spread, config.lookback)
    valid_hl = [h for h in half_lives if h is not None and h < 1000]
    avg_half_life = float(np.mean(valid_hl)) if valid_hl else config.default_max_hold

    # --- Enhancement 4: Rolling ADF regime detection ---
    if config.use_regime_filter:
        rolling_adf = rolling_adf_pvalue(dynamic_spread, config.regime_adf_window)
    else:
        rolling_adf = [None] * len(dynamic_spread)

    # --- Z-scores ---
    zscores = rolling_zscore(dynamic_spread, config.lookback)

    # --- Trading simulation ---
    trades: list[Trade] = []
    equity = CAPITAL * config.position_pct * 2
    equity_curve = [equity]
    in_position = False
    pos_direction = ""
    entry_idx = 0
    entry_z = 0.0
    entry_spread = 0.0
    entry_price_a = 0.0
    entry_price_b = 0.0
    entry_beta = 1.0
    regime_filtered_count = 0
    total_evaluated = 0

    for i in range(config.lookback, len(common_ts)):
        z = zscores[i]
        if z is None:
            equity_curve.append(equity_curve[-1])
            continue

        total_evaluated += 1

        # Enhancement 4: Regime filter — skip if spread is non-stationary
        if config.use_regime_filter and rolling_adf[i] is not None:
            if rolling_adf[i] > config.regime_adf_threshold:
                regime_filtered_count += 1
                # Force exit if in position and regime breaks
                if in_position:
                    exit_price_a = prices_a[i]
                    exit_price_b = prices_b[i]
                    if pos_direction == "long_A_short_B":
                        pnl_a = (exit_price_a - entry_price_a) / entry_price_a
                        pnl_b = (entry_price_b - exit_price_b) / entry_price_b
                    else:
                        pnl_a = (entry_price_a - exit_price_a) / entry_price_a
                        pnl_b = (exit_price_b - entry_price_b) / entry_price_b
                    # Weight by hedge ratio
                    gross_pnl = (pnl_a + pnl_b * entry_beta) / (1 + entry_beta)
                    comm = COMMISSION * 4
                    net_pnl = gross_pnl - comm
                    equity *= (1 + net_pnl)
                    trades.append(Trade(
                        entry_day=entry_idx, exit_day=i,
                        direction=pos_direction + "_regime_exit",
                        entry_z=entry_z, exit_z=z,
                        entry_spread=entry_spread, exit_spread=dynamic_spread[i],
                        pnl_pct=net_pnl * 100,
                        commission_pct=comm * 100,
                    ))
                    in_position = False
                equity_curve.append(equity_curve[-1])
                continue

        if in_position:
            should_exit = False
            exit_reason = ""

            # Take profit
            if pos_direction == "long_A_short_B" and z <= config.exit_z:
                should_exit = True
                exit_reason = "tp"
            elif pos_direction == "short_A_long_B" and z >= -config.exit_z:
                should_exit = True
                exit_reason = "tp"

            # Stop loss
            if pos_direction == "long_A_short_B" and z >= config.stop_z:
                should_exit = True
                exit_reason = "stop"
            elif pos_direction == "short_A_long_B" and z <= -config.stop_z:
                should_exit = True
                exit_reason = "stop"

            # Enhancement 3: Adaptive max hold based on half-life
            if config.use_halflife_exit and half_lives[i] is not None and half_lives[i] < 500:
                adaptive_max_hold = int(half_lives[i] * config.max_hold_multiplier)
                adaptive_max_hold = max(5, min(adaptive_max_hold, 60))
            else:
                adaptive_max_hold = config.default_max_hold

            if i - entry_idx >= adaptive_max_hold:
                should_exit = True
                exit_reason = "maxhold"

            if should_exit:
                exit_price_a = prices_a[i]
                exit_price_b = prices_b[i]

                if pos_direction == "long_A_short_B":
                    pnl_a = (exit_price_a - entry_price_a) / entry_price_a
                    pnl_b = (entry_price_b - exit_price_b) / entry_price_b
                else:
                    pnl_a = (entry_price_a - exit_price_a) / entry_price_a
                    pnl_b = (exit_price_b - entry_price_b) / entry_price_b

                # Weight PnL by hedge ratio
                gross_pnl = (pnl_a + pnl_b * abs(entry_beta)) / (1 + abs(entry_beta))
                comm = COMMISSION * 4
                net_pnl = gross_pnl - comm
                equity *= (1 + net_pnl)

                trades.append(Trade(
                    entry_day=entry_idx, exit_day=i,
                    direction=pos_direction + "_" + exit_reason,
                    entry_z=entry_z, exit_z=z,
                    entry_spread=entry_spread, exit_spread=dynamic_spread[i],
                    pnl_pct=net_pnl * 100,
                    commission_pct=comm * 100,
                ))
                in_position = False

        if not in_position:
            if z >= config.entry_z:
                in_position = True
                pos_direction = "short_A_long_B"
                entry_idx = i
                entry_z = z
                entry_spread = dynamic_spread[i]
                entry_price_a = prices_a[i]
                entry_price_b = prices_b[i]
                entry_beta = betas[i] if config.use_kalman else 1.0
            elif z <= -config.entry_z:
                in_position = True
                pos_direction = "long_A_short_B"
                entry_idx = i
                entry_z = z
                entry_spread = dynamic_spread[i]
                entry_price_a = prices_a[i]
                entry_price_b = prices_b[i]
                entry_beta = betas[i] if config.use_kalman else 1.0

        equity_curve.append(equity)

    # Compute stats
    num_days = len(common_ts)
    total_return = (equity_curve[-1] / equity_curve[0] - 1) * 100 if equity_curve[0] > 0 else 0
    monthly_return = total_return / (num_days / 30) if num_days > 0 else 0

    peak = equity_curve[0]
    max_dd = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    bar_rets = [equity_curve[j] / equity_curve[j-1] - 1 for j in range(1, len(equity_curve))]
    sharpe = 0.0
    sortino = 0.0
    if bar_rets:
        avg_r = sum(bar_rets) / len(bar_rets)
        var_r = sum((r - avg_r)**2 for r in bar_rets) / len(bar_rets)
        std_r = math.sqrt(var_r) if var_r > 0 else 0
        if std_r > 0:
            sharpe = avg_r / std_r * math.sqrt(365)
        downside = [r for r in bar_rets if r < 0]
        if downside:
            ds_var = sum(r**2 for r in downside) / len(downside)
            ds_std = math.sqrt(ds_var)
            if ds_std > 0:
                sortino = avg_r / ds_std * math.sqrt(365)

    regime_filtered_pct = regime_filtered_count / max(1, total_evaluated) * 100

    return EnhancedResult(
        config=config, trades=trades,
        total_return_pct=total_return,
        monthly_return_pct=monthly_return,
        max_dd_pct=max_dd,
        sharpe=sharpe, sortino=sortino,
        correlation=correlation,
        adf_pvalue=adf_result["pvalue"],
        coint_pvalue=coint_result["pvalue"],
        avg_half_life=avg_half_life,
        avg_hedge_ratio=avg_beta,
        regime_filtered_pct=regime_filtered_pct,
        num_days=num_days,
        equity_curve=equity_curve,
    )


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def compute_stats(equity_curve: list[float], bars_per_year: int = 365) -> dict:
    if len(equity_curve) < 2:
        return {"return": 0, "monthly": 0, "sharpe": 0, "sortino": 0, "max_dd": 0}
    total_ret = (equity_curve[-1] / equity_curve[0] - 1) * 100
    monthly = total_ret / (len(equity_curve) / (bars_per_year / 12))
    peak = equity_curve[0]
    max_dd = 0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd: max_dd = dd
    rets = [equity_curve[i]/equity_curve[i-1]-1 for i in range(1, len(equity_curve))]
    if not rets:
        return {"return": total_ret, "monthly": monthly, "sharpe": 0, "sortino": 0, "max_dd": max_dd}
    avg = sum(rets)/len(rets)
    var = sum((r-avg)**2 for r in rets)/len(rets)
    std = math.sqrt(var) if var > 0 else 0
    sharpe = avg/std*math.sqrt(bars_per_year) if std > 0 else 0
    ds = [r for r in rets if r < 0]
    sortino = avg/math.sqrt(sum(r**2 for r in ds)/len(ds))*math.sqrt(bars_per_year) if ds else sharpe*2
    return {"return": total_ret, "monthly": monthly, "sharpe": sharpe, "sortino": sortino, "max_dd": max_dd}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    quick = "--quick" in sys.argv

    print(f"=== Enhanced Pairs Trading Backtest ({DAYS} days, ${CAPITAL:.0f}, comm={COMMISSION*100:.3f}%) ===")
    print(f"Improvements: ADF selection, Kalman hedge, OU half-life exit, Rolling ADF regime filter")
    print()

    # Load data
    symbols = ["LINKUSDT", "SOLUSDT", "ETHUSDT", "UNIUSDT", "LTCUSDT"]
    price_data = {}
    for sym in symbols:
        exchange = SYMBOL_SOURCES[sym]
        data = load_daily_closes(sym, exchange, DAYS)
        if data:
            price_data[sym] = data
            d0 = datetime.fromtimestamp(data[0][0], tz=timezone.utc).strftime("%Y-%m-%d")
            d1 = datetime.fromtimestamp(data[-1][0], tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"  {sym:12s} ({exchange:8s}): {len(data):>4d} days  {d0} → {d1}")
    print()

    # --- Step 1: Cointegration screening for all pairs ---
    print("=== Step 1: Cointegration Screening ===")
    all_pairs = [
        ("LINKUSDT", "SOLUSDT"), ("ETHUSDT", "UNIUSDT"), ("LINKUSDT", "LTCUSDT"),
        ("LINKUSDT", "ETHUSDT"), ("LINKUSDT", "UNIUSDT"), ("ETHUSDT", "SOLUSDT"),
        ("SOLUSDT", "UNIUSDT"), ("LINKUSDT", "XRPUSDT"), ("BTCUSDT", "ETHUSDT"),
    ]
    # Only test pairs where we have data
    testable_pairs = [(a, b) for a, b in all_pairs if a in price_data and b in price_data]

    pair_stats = []
    for a, b in testable_pairs:
        ts_a = dict(price_data[a])
        ts_b = dict(price_data[b])
        common = sorted(set(ts_a.keys()) & set(ts_b.keys()))
        pa = [ts_a[t] for t in common]
        pb = [ts_b[t] for t in common]

        if len(pa) < 100:
            continue

        corr = float(np.corrcoef(np.diff(pa)/pa[:-1], np.diff(pb)/pb[:-1])[0, 1])
        spread = [math.log(a/b) for a, b in zip(pa, pb)]
        adf_r = adf_test(spread)
        coint_r = cointegration_test(pa, pb)
        hl = estimate_half_life(spread)

        label = f"{a.replace('USDT','')}/{b.replace('USDT','')}"
        pair_stats.append({
            "pair": (a, b), "label": label,
            "corr": corr, "adf_p": adf_r["pvalue"], "coint_p": coint_r["pvalue"],
            "half_life": hl,
            "cointegrated": adf_r["pvalue"] < 0.10 or coint_r["pvalue"] < 0.10,
        })
        print(f"  {label:15s}  corr={corr:+.3f}  ADF p={adf_r['pvalue']:.3f}  Coint p={coint_r['pvalue']:.3f}  HL={hl:.1f}d  {'✓ COINT' if pair_stats[-1]['cointegrated'] else '✗ not coint'}")

    # Filter to cointegrated pairs
    coint_pairs = [p for p in pair_stats if p["cointegrated"]]
    print(f"\n  {len(coint_pairs)}/{len(pair_stats)} pairs pass cointegration test")
    print()

    # --- Step 2: Backtest with enhancements ---
    print("=== Step 2: Enhanced Backtest Results ===")
    print()

    # Test configurations
    test_configs = []

    # For each cointegrated pair, test a range of parameters
    for ps in coint_pairs:
        a, b = ps["pair"]
        if quick:
            configs_for_pair = [
                EnhancedConfig(pair=(a, b), lookback=20, entry_z=2.5, exit_z=0.0, label=f"{ps['label']} L20E2.5 ALL"),
                EnhancedConfig(pair=(a, b), lookback=20, entry_z=2.5, exit_z=0.0, use_kalman=False, use_halflife_exit=False, use_regime_filter=False, label=f"{ps['label']} L20E2.5 BASIC"),
            ]
        else:
            configs_for_pair = []
            for lookback in [15, 20, 30]:
                for entry_z in [2.0, 2.5, 3.0]:
                    # Enhanced (all improvements ON)
                    configs_for_pair.append(EnhancedConfig(
                        pair=(a, b), lookback=lookback, entry_z=entry_z, exit_z=0.0,
                        label=f"{ps['label']} L{lookback}E{entry_z} ALL",
                    ))
                    # Baseline (all improvements OFF)
                    configs_for_pair.append(EnhancedConfig(
                        pair=(a, b), lookback=lookback, entry_z=entry_z, exit_z=0.0,
                        use_kalman=False, use_halflife_exit=False, use_regime_filter=False,
                        label=f"{ps['label']} L{lookback}E{entry_z} BASIC",
                    ))

        test_configs.extend(configs_for_pair)

    # Run all configs
    results: list[EnhancedResult] = []
    for cfg in test_configs:
        a, b = cfg.pair
        if a not in price_data or b not in price_data:
            continue
        r = backtest_enhanced_pair(cfg, price_data[a], price_data[b])
        results.append(r)

    # Sort by Sharpe
    results.sort(key=lambda r: r.sharpe, reverse=True)

    # Print top results
    print(f"  Tested {len(results)} configurations")
    print()
    print(f"  {'Config':<30s} │ {'Return':>8s} {'Monthly':>8s} {'Trades':>7s} {'WR%':>6s} │ {'MaxDD':>7s} {'Sharpe':>7s} {'Sort':>6s} │ {'HL':>5s} {'β':>5s} {'Rég%':>5s} │ ADF p")
    print(f"  {'-'*120}")

    for r in results[:40]:
        if not r.trades:
            continue
        cfg = r.config
        wr = sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) * 100
        hl_str = f"{r.avg_half_life:.0f}" if r.avg_half_life < 1000 else "∞"
        print(
            f"  {cfg.label:<30s} │ {r.total_return_pct:>+7.1f}% {r.monthly_return_pct:>+7.2f}% {len(r.trades):>7d} {wr:>5.1f}% │"
            f" {r.max_dd_pct:>6.1f}% {r.sharpe:>+6.2f} {r.sortino:>+5.2f} │"
            f" {hl_str:>5s} {r.avg_hedge_ratio:>5.2f} {r.regime_filtered_pct:>4.0f}% │ {r.adf_pvalue:.3f}"
        )

    # --- Step 3: Walk-forward validation on best configs ---
    print()
    print("=== Step 3: Walk-Forward Validation (Best Configs) ===")
    print()

    # Deduplicate and take best per pair
    seen = set()
    best_per_pair = []
    for r in results:
        key = (r.config.pair, "ALL" if r.config.use_kalman else "BASIC")
        if key not in seen:
            seen.add(key)
            best_per_pair.append(r)
        if len(best_per_pair) >= 12:
            break

    print(f"  {'Config':<30s} │ {'Full':>25s} │ {'1st Half':>15s} │ {'2nd Half':>15s}")
    print(f"  {'':30s} │ {'Return  Sharpe  Tr':>25s} │ {'Ret  Sharpe':>15s} │ {'Ret  Sharpe':>15s}")
    print(f"  {'-'*100}")

    for r in best_per_pair:
        if not r.equity_curve or len(r.equity_curve) < 10:
            continue
        cfg = r.config
        a, b = cfg.pair

        # Re-run on halves
        ts_a = dict(price_data[a])
        ts_b = dict(price_data[b])
        common = sorted(set(ts_a.keys()) & set(ts_b.keys()))
        mid = len(common) // 2

        # Split data
        dates_a = price_data[a]
        dates_b = price_data[b]
        da1 = [(t, p) for t, p in dates_a if t <= common[mid]]
        da2 = [(t, p) for t, p in dates_a if t > common[mid]]
        db1 = [(t, p) for t, p in dates_b if t <= common[mid]]
        db2 = [(t, p) for t, p in dates_b if t > common[mid]]

        r1 = backtest_enhanced_pair(cfg, da1, db1)
        r2 = backtest_enhanced_pair(cfg, da2, db2)

        wr = sum(1 for t in r.trades if t.pnl_pct > 0) / max(1, len(r.trades)) * 100
        both = "✓" if r1.total_return_pct > 0 and r2.total_return_pct > 0 else "✗"

        print(f"  {cfg.label:<30s} │ {r.total_return_pct:>+7.1f}%  {r.sharpe:>+5.2f}  {len(r.trades):>4d}  │ {r1.total_return_pct:>+6.1f}%  {r1.sharpe:>+5.2f}  │ {r2.total_return_pct:>+6.1f}%  {r2.sharpe:>+5.2f}  {both}")

    # --- Step 4: Enhancement Ablation ---
    print()
    print("=== Step 4: Enhancement Ablation (Best Pair) ===")
    print()

    if results and results[0].trades:
        best = results[0]
        a, b = best.config.pair

        ablation_configs = [
            ("ALL ON", EnhancedConfig(pair=(a, b), lookback=best.config.lookback, entry_z=best.config.entry_z, exit_z=0.0, label="ALL ON")),
            ("No Kalman", EnhancedConfig(pair=(a, b), lookback=best.config.lookback, entry_z=best.config.entry_z, exit_z=0.0, use_kalman=False, label="No Kalman")),
            ("No HalfLife", EnhancedConfig(pair=(a, b), lookback=best.config.lookback, entry_z=best.config.entry_z, exit_z=0.0, use_halflife_exit=False, label="No HalfLife")),
            ("No Regime", EnhancedConfig(pair=(a, b), lookback=best.config.lookback, entry_z=best.config.entry_z, exit_z=0.0, use_regime_filter=False, label="No Regime")),
            ("BASIC (all off)", EnhancedConfig(pair=(a, b), lookback=best.config.lookback, entry_z=best.config.entry_z, exit_z=0.0, use_kalman=False, use_halflife_exit=False, use_regime_filter=False, label="BASIC")),
        ]

        print(f"  Best pair: {a}/{b}")
        print(f"  {'Config':<20s} │ {'Return':>8s} {'Sharpe':>7s} {'Trades':>7s} {'WR%':>6s} {'MaxDD':>7s} │ {'HL':>5s} {'β':>5s} {'Rég%':>5s}")
        print(f"  {'-'*90}")

        for label, cfg in ablation_configs:
            r = backtest_enhanced_pair(cfg, price_data[a], price_data[b])
            if not r.trades:
                print(f"  {label:<20s} │ NO TRADES")
                continue
            wr = sum(1 for t in r.trades if t.pnl_pct > 0) / len(r.trades) * 100
            hl_str = f"{r.avg_half_life:.0f}" if r.avg_half_life < 1000 else "∞"
            print(
                f"  {label:<20s} │ {r.total_return_pct:>+7.1f}% {r.sharpe:>+6.2f} {len(r.trades):>7d} {wr:>5.1f}% {r.max_dd_pct:>6.1f}% │"
                f" {hl_str:>5s} {r.avg_hedge_ratio:>5.2f} {r.regime_filtered_pct:>4.0f}%"
            )


if __name__ == "__main__":
    main()
