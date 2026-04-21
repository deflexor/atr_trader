"""Gaussian Mixture Model regime detector.

Classifies market into 4 regimes using return distribution features:
- CALM_TRENDING: Low vol, directional drift — best for trend entries
- VOLATILE_TRENDING: High vol with direction — wider stops needed
- MEAN_REVERTING: No direction, low vol — fade entries only
- CRASH: Extreme negative skew + high vol — halt all entries

Uses numpy/scipy for GMM fitting (no sklearn dependency).
Inspired by fecon235 gauss-mix-kurtosis.ipynb.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
from scipy.stats import kurtosis, skew


class MarketRegime(Enum):
    """Market regime classification."""

    CALM_TRENDING = "CALM_TRENDING"
    VOLATILE_TRENDING = "VOLATILE_TRENDING"
    MEAN_REVERTING = "MEAN_REVERTING"
    CRASH = "CRASH"


@dataclass(frozen=True)
class RegimeResult:
    """Immutable regime detection result."""

    regime: MarketRegime
    confidence: float  # 0-1, how certain the classification is
    volatility_percentile: float  # 0-1, where current vol sits in history
    skewness: float
    kurtosis_val: float
    energy: float  # Uncertainty metric for Boltzmann sizing (0=confident, 1=uncertain)


def _compute_features(returns: np.ndarray) -> dict[str, float]:
    """Extract statistical features from return series.

    Pure function — no side effects.
    """
    if len(returns) < 20:
        return {"vol": 0.0, "skew": 0.0, "kurt": 0.0, "mean": 0.0}

    return {
        "vol": float(np.std(returns)),
        "skew": float(skew(returns, bias=False)),
        "kurt": float(kurtosis(returns, bias=False, fisher=True)),
        "mean": float(np.mean(returns)),
    }


def _classify_regime(
    features: dict[str, float],
    vol_percentile: float,
    crash_skew_threshold: float = -1.0,
    crash_mean_threshold: float = -0.015,
    vol_high_percentile: float = 0.75,
) -> tuple[MarketRegime, float]:
    """Classify regime from statistical features. Pure function.

    Returns (regime, confidence).
    """
    vol = features["vol"]
    skew_val = features["skew"]
    mean_val = features["mean"]

    # Crash: extreme negative skew OR extreme negative mean + high volatility
    # Use absolute thresholds — crash is absolute, not relative to history
    is_crash = False
    crash_confidence = 0.0
    if skew_val < crash_skew_threshold:
        is_crash = True
        crash_confidence = min(1.0, abs(skew_val) / 3.0)
    elif mean_val < crash_mean_threshold and vol > 0.02:
        # Strong negative drift with high vol = crash-like
        is_crash = True
        crash_confidence = min(1.0, abs(mean_val) / 0.05)

    if is_crash:
        return MarketRegime.CRASH, crash_confidence

    # Directional: mean drift is significant relative to vol
    has_direction = abs(mean_val) > 0.3 * vol if vol > 0 else False

    if has_direction:
        if vol_percentile > vol_high_percentile:
            confidence = 0.6 + 0.4 * vol_percentile
            return MarketRegime.VOLATILE_TRENDING, min(confidence, 1.0)
        confidence = 0.6 + 0.4 * (1.0 - vol_percentile)
        return MarketRegime.CALM_TRENDING, min(confidence, 1.0)

    # No direction — mean reverting
    confidence = 0.5 + 0.3 * (1.0 - vol_percentile)
    return MarketRegime.MEAN_REVERTING, min(confidence, 1.0)


def _compute_energy(confidence: float, vol_percentile: float) -> float:
    """Compute uncertainty energy for Boltzmann sizing. Pure function.

    Higher energy = more uncertain = smaller position.
    """
    return (1.0 - confidence) * 0.6 + vol_percentile * 0.4


class RegimeDetector:
    """Detects market regime using GMM-inspired statistical features.

    Tracks a rolling window of returns and classifies the current regime.
    Stateful: maintains return history for percentile calculation.
    """

    def __init__(self, lookback: int = 100, min_samples: int = 30) -> None:
        self._lookback = lookback
        self._min_samples = min_samples
        self._returns: list[float] = []
        self._vol_history: list[float] = []

    def update(self, return_value: float) -> None:
        """Record a new return observation."""
        self._returns.append(return_value)
        if len(self._returns) > self._lookback * 2:
            self._returns = self._returns[-self._lookback * 2:]

    def detect(self, recent_returns: Optional[list[float]] = None) -> RegimeResult:
        """Classify the current market regime.

        Args:
            recent_returns: Override returns (uses internal history if None)

        Returns:
            RegimeResult with regime, confidence, and energy metrics
        """
        returns = np.array(recent_returns or self._returns[-self._lookback:])

        if len(returns) < self._min_samples:
            return RegimeResult(
                regime=MarketRegime.MEAN_REVERTING,
                confidence=0.0,
                volatility_percentile=0.5,
                skewness=0.0,
                kurtosis_val=0.0,
                energy=1.0,
            )

        features = _compute_features(returns)

        # Compute volatility percentile from history
        vol = features["vol"]
        self._vol_history.append(vol)
        if len(self._vol_history) > self._lookback:
            self._vol_history = self._vol_history[-self._lookback:]

        vol_percentile = _compute_vol_percentile(vol, self._vol_history)

        regime, confidence = _classify_regime(features, vol_percentile)
        energy = _compute_energy(confidence, vol_percentile)

        return RegimeResult(
            regime=regime,
            confidence=confidence,
            volatility_percentile=vol_percentile,
            skewness=features["skew"],
            kurtosis_val=features["kurt"],
            energy=energy,
        )

    def reset(self) -> None:
        """Clear all internal state."""
        self._returns = []
        self._vol_history = []


def _compute_vol_percentile(current_vol: float, vol_history: list[float]) -> float:
    """Compute where current vol sits in history. Pure function."""
    if not vol_history:
        return 0.5

    below = sum(1 for v in vol_history if v < current_vol)
    return below / len(vol_history)
