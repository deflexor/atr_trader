"""Boltzmann position sizer.

Uses thermal weighting inspired by fecon235's Boltzmann portfolios
(prtf-boltzmann-1.ipynb) to adjust position size based on regime
uncertainty (energy).

Formula: size = base_size * exp(-energy / temperature)

Where:
- energy: uncertainty from regime detection (0=confident, 1=uncertain)
- temperature: controls sensitivity to uncertainty

In CALM_TRENDING (low energy): full position size
In VOLATILE_TRENDING (medium energy): reduced size
In CRASH (high energy): minimal or zero size

This is the core innovation from Boltzmann portfolio theory: instead of
Markowitz mean-variance optimization (which is unstable), use thermal
weighting that naturally reduces exposure in uncertain regimes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .regime_detector import MarketRegime, RegimeResult


@dataclass(frozen=True)
class BoltzmannConfig:
    """Immutable Boltzmann sizing configuration."""

    temperature: float = 0.3  # Controls sensitivity to energy
    min_size_fraction: float = 0.05  # Minimum position (5% of base)
    max_size_fraction: float = 1.0  # Maximum position (100% of base)

    # Per-regime caps (override thermal calculation)
    regime_caps: dict = None  # Set via __post_init__ if needed

    def __post_init__(self) -> None:
        # Can't set frozen dataclass attributes, so use property
        pass

    @property
    def effective_regime_caps(self) -> dict[MarketRegime, float]:
        """Default regime caps if not overridden."""
        return {
            MarketRegime.CALM_TRENDING: 1.0,
            MarketRegime.VOLATILE_TRENDING: 0.5,
            MarketRegime.MEAN_REVERTING: 0.3,
            MarketRegime.CRASH: 0.0,
        }


def _boltzmann_fraction(energy: float, temperature: float) -> float:
    """Compute Boltzmann weighting fraction. Pure function.

    Formula: f = exp(-energy / temperature)

    At energy=0 (confident): f = 1.0 (full size)
    At energy=1 (uncertain): f = exp(-1/temperature)

    Args:
        energy: Uncertainty metric (0-1)
        temperature: Sensitivity parameter

    Returns:
        Size fraction (0-1)
    """
    if temperature <= 0:
        return 0.0
    return float(math.exp(-energy / temperature))


class BoltzmannPositionSizer:
    """Adjusts position size using Boltzmann thermal weighting.

    Combines thermal weighting with regime-specific caps for safety.
    """

    def __init__(self, config: BoltzmannConfig | None = None) -> None:
        self._config = config or BoltzmannConfig()

    def calculate_size_fraction(self, regime_result: RegimeResult) -> float:
        """Calculate position size fraction based on regime.

        Args:
            regime_result: Current regime classification with energy

        Returns:
            Size fraction (0-1) to multiply against base position size
        """
        # Check regime cap first (hard limit)
        caps = self._config.effective_regime_caps
        regime_cap = caps.get(regime_result.regime, 0.3)

        if regime_cap <= 0:
            return 0.0

        # Compute thermal weighting
        thermal = _boltzmann_fraction(regime_result.energy, self._config.temperature)

        # Apply regime cap
        fraction = min(thermal, regime_cap)

        # Clamp to configured bounds
        return max(self._config.min_size_fraction, min(fraction, self._config.max_size_fraction))

    def adjust_position_value(
        self,
        base_position_value: float,
        regime_result: RegimeResult,
    ) -> float:
        """Adjust a base position value by regime-based thermal weighting.

        Args:
            base_position_value: Original position size in currency
            regime_result: Current regime classification

        Returns:
            Adjusted position value
        """
        fraction = self.calculate_size_fraction(regime_result)
        return base_position_value * fraction
