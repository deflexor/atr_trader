"""Bootstrap stop calculator.

Computes stop-loss distances using bootstrap resampling of recent returns.
Instead of using a single ATR multiplier, resamples the return distribution
N times to estimate worst-case drawdown at a given confidence level.

Inspired by fecon235 boots-eq-spx.ipynb (bootstrap leptokurtotic returns).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class BootstrapStopResult:
    """Immutable bootstrap stop calculation result."""

    stop_distance_pct: float  # Stop distance as % of entry price
    confidence_level: float  # Confidence level used (e.g. 0.95)
    worst_case_pct: float  # Worst-case drawdown at confidence level
    n_simulations: int  # Number of bootstrap simulations run
    sample_size: int  # Number of returns in the sample


def _bootstrap_drawdowns(
    returns: np.ndarray,
    n_simulations: int = 1000,
    horizon: int = 20,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Generate bootstrap drawdown distribution. Pure function.

    Resamples returns with replacement and computes max drawdown
    for each simulated path.

    Args:
        returns: Historical log returns
        n_simulations: Number of bootstrap samples
        horizon: Forward-looking periods to simulate
        rng: Optional numpy random generator for reproducibility

    Returns:
        Array of max drawdown values (as positive percentages)
    """
    if rng is None:
        rng = np.random.default_rng()

    drawdowns = np.empty(n_simulations)

    for i in range(n_simulations):
        # Resample with replacement
        sampled = rng.choice(returns, size=horizon, replace=True)

        # Compute equity curve and max drawdown
        equity = np.cumprod(1.0 + sampled)
        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak
        drawdowns[i] = np.max(dd)

    return drawdowns


def compute_bootstrap_stop(
    returns: list[float],
    confidence_level: float = 0.95,
    n_simulations: int = 1000,
    horizon: int = 20,
    min_stop_pct: float = 0.003,
    max_stop_pct: float = 0.05,
    seed: Optional[int] = None,
) -> BootstrapStopResult:
    """Calculate stop-loss distance from bootstrapped worst-case drawdown.

    Pure function — no side effects.

    Args:
        returns: Recent return series (log or simple returns)
        confidence_level: Quantile for worst-case (0.95 = 95th percentile)
        n_simulations: Number of bootstrap samples
        horizon: Forward periods to simulate
        min_stop_pct: Minimum stop distance (0.3%)
        max_stop_pct: Maximum stop distance (5%)
        seed: Random seed for reproducibility

    Returns:
        BootstrapStopResult with stop distance and metadata
    """
    if len(returns) < 20:
        # Fallback to simple volatility-based stop
        vol = float(np.std(returns)) if len(returns) > 1 else 0.01
        fallback = max(min_stop_pct, min(2.0 * vol, max_stop_pct))
        return BootstrapStopResult(
            stop_distance_pct=fallback,
            confidence_level=confidence_level,
            worst_case_pct=fallback,
            n_simulations=0,
            sample_size=len(returns),
        )

    rng = np.random.default_rng(seed) if seed is not None else None
    returns_arr = np.array(returns)

    drawdowns = _bootstrap_drawdowns(returns_arr, n_simulations, horizon, rng)

    worst_case = float(np.percentile(drawdowns, confidence_level * 100))

    # Clamp to reasonable range
    stop_pct = max(min_stop_pct, min(worst_case, max_stop_pct))

    return BootstrapStopResult(
        stop_distance_pct=stop_pct,
        confidence_level=confidence_level,
        worst_case_pct=worst_case,
        n_simulations=n_simulations,
        sample_size=len(returns),
    )


class BootstrapStopCalculator:
    """Stateful bootstrap stop calculator for incremental updates.

    Maintains a rolling window of returns and computes stops on demand.
    """

    def __init__(
        self,
        lookback: int = 100,
        confidence_level: float = 0.95,
        n_simulations: int = 1000,
        horizon: int = 20,
        min_stop_pct: float = 0.003,
        max_stop_pct: float = 0.05,
    ) -> None:
        self._lookback = lookback
        self._confidence = confidence_level
        self._n_sim = n_simulations
        self._horizon = horizon
        self._min_stop = min_stop_pct
        self._max_stop = max_stop_pct
        self._returns: list[float] = []

    def update(self, return_value: float) -> None:
        """Record a new return observation."""
        self._returns.append(return_value)
        if len(self._returns) > self._lookback:
            self._returns = self._returns[-self._lookback:]

    def calculate(self, seed: Optional[int] = None) -> BootstrapStopResult:
        """Compute bootstrap stop from accumulated returns."""
        return compute_bootstrap_stop(
            returns=self._returns,
            confidence_level=self._confidence,
            n_simulations=self._n_sim,
            horizon=self._horizon,
            min_stop_pct=self._min_stop,
            max_stop_pct=self._max_stop,
            seed=seed,
        )

    def reset(self) -> None:
        """Clear return history."""
        self._returns = []
