"""Adaptive position sizer based on unrealized P&L and market regime.

Gradually reduces position size as unrealized losses grow during
dangerous market conditions. Remains inactive in calm/mean-reverting
markets where temporary dips are normal and positions typically recover.

This "regime-aware soft stop" approach:
- Only activates during CRASH and VOLATILE_TRENDING regimes
- Stays hands-off during CALM_TRENDING and MEAN_REVERTING (89% of BTC)
- Uses tighter thresholds when regime energy is high

Backtesting showed that static thresholds (-1%/-2%/-3%) cut winners
that would recover, killing win rate from 60% to 8%. The fix: only
reduce when the market regime confirms the loss is likely structural,
not noise.

Regime-aware graduated scale (default):
    CRASH:              -2% → 25%,  -3% → 50%,  -5% → close
    VOLATILE_TRENDING:  -3% → 25%,  -5% → 50%,  -8% → close
    CALM_TRENDING:      no reduction (let winners breathe)
    MEAN_REVERTING:     no reduction (dips are normal)

Inspired by Phase 1 observation: the 9% max drawdown comes from
sudden adverse moves inside open positions during market stress.
Pre-trade filters cannot prevent this — only intra-trade management
that's aware of market context can help.
"""

from __future__ import annotations

from dataclasses import dataclass

from .regime_detector import MarketRegime


@dataclass(frozen=True)
class AdaptiveSizerConfig:
    """Immutable adaptive sizing configuration.

    Attributes:
        regime_thresholds: Per-regime graduated (pnl_pct, reduce_fraction) tuples.
            Only regimes present in this dict will trigger reductions.
            Omit a regime to disable adaptive sizing for it.
        cooldown_candles: Minimum candles between partial closes.
        min_energy: Only activate when regime energy exceeds this (0-1).
            Filters out low-confidence regime classifications.
    """

    regime_thresholds: dict[MarketRegime, tuple[tuple[float, float], ...]] = None
    cooldown_candles: int = 15
    min_energy: float = 0.3

    def __post_init__(self) -> None:
        if self.regime_thresholds is None:
            object.__setattr__(self, "regime_thresholds", {
                MarketRegime.CRASH: (
                    (-2.0, 0.25),
                    (-3.0, 0.50),
                    (-5.0, 1.0),
                ),
                MarketRegime.VOLATILE_TRENDING: (
                    (-3.0, 0.25),
                    (-5.0, 0.50),
                    (-8.0, 1.0),
                ),
            })

    @property
    def active_regimes(self) -> set[MarketRegime]:
        return set(self.regime_thresholds.keys())


def _compute_reduce_fraction(
    unrealized_pnl_pct: float,
    thresholds: tuple[tuple[float, float], ...],
) -> float:
    """Compute position reduction fraction based on unrealized P&L. Pure function.

    Returns the reduce_fraction from the most severe threshold whose
    loss level has been reached. If no threshold is triggered, returns 0.0.

    Args:
        unrealized_pnl_pct: Unrealized P&L as percentage (negative = loss).
        thresholds: Escalating (pnl_pct, reduce_fraction) pairs.

    Returns:
        Fraction of position to reduce (0.0 = none, 1.0 = close all).
    """
    fraction = 0.0
    for threshold_pct, reduce_frac in thresholds:
        if unrealized_pnl_pct <= threshold_pct:
            fraction = reduce_frac
    return fraction


class AdaptivePositionSizer:
    """Reduces positions gradually during dangerous market regimes.

    Only activates when the current regime is in the configured
    regime_thresholds AND the regime energy exceeds min_energy.
    This prevents cutting winners during normal market noise.
    """

    def __init__(self, config: AdaptiveSizerConfig | None = None) -> None:
        self._config = config or AdaptiveSizerConfig()

    def evaluate(
        self,
        unrealized_pnl_pct: float,
        candles_since_last_reduce: int,
        regime: MarketRegime | None = None,
        regime_energy: float = 0.0,
    ) -> float:
        """Evaluate whether to reduce position based on P&L and regime.

        Args:
            unrealized_pnl_pct: Current unrealized P&L % (negative = loss).
            candles_since_last_reduce: Candles since last partial close.
            regime: Current market regime (from RegimeDetector).
            regime_energy: Regime uncertainty (0=confident, 1=uncertain).

        Returns:
            Fraction of position to reduce (0.0 = no action).
        """
        # Skip if in cooldown
        if candles_since_last_reduce < self._config.cooldown_candles:
            return 0.0

        # Skip if regime is not in active set
        if regime is None or regime not in self._config.active_regimes:
            return 0.0

        # Skip if regime energy is too low (low-confidence classification)
        if regime_energy < self._config.min_energy:
            return 0.0

        thresholds = self._config.regime_thresholds.get(regime)
        if thresholds is None:
            return 0.0

        return _compute_reduce_fraction(unrealized_pnl_pct, thresholds)

    @property
    def config(self) -> AdaptiveSizerConfig:
        return self._config
