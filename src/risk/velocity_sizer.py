"""Velocity-based adaptive position sizer.

Reduces positions when unrealized P&L is dropping rapidly (high velocity),
rather than when it reaches a fixed absolute level. This avoids cutting
winners that are in a normal pullback while catching positions that are
in an accelerating adverse move.

Phase 2 showed that absolute P&L thresholds (-1%/-2%/-3%) kill win rate
because normal BTC noise hits those levels routinely. The velocity
approach asks: "how fast is this position losing money?" not "how much
has it lost?"

Design:
    - Velocity = slope of unrealized P&L over last N candles (%/candle)
    - A position at -2% with velocity ≈ 0 is stable — leave it alone
    - A position at -1% with velocity -0.8%/candle is in trouble — reduce
    - Acceleration (velocity of velocity) amplifies the signal
    - Regime gate: only activates during CRASH and VOLATILE_TRENDING
    - Minimum P&L guard: must be losing to trigger (no false positives on flat positions)

Default velocity thresholds:
    CRASH:              -0.5%/c → 25%,  -1.0%/c → 50%,  -2.0%/c → close
    VOLATILE_TRENDING:  -0.3%/c → 25%,  -0.6%/c → 50%,  -1.2%/c → close

Acceleration multiplier:
    If velocity is accelerating (acceleration < 0), scale reduce fraction up
    by up to 1.5x. A position losing faster and faster is more dangerous
    than one losing at a constant rate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .regime_detector import MarketRegime
from .velocity_tracker import VelocityResult


@dataclass(frozen=True)
class VelocitySizerConfig:
    """Immutable velocity sizing configuration.

    Attributes:
        regime_velocity_thresholds: Per-regime (velocity_pct_per_candle, reduce_fraction) tuples.
            Velocity thresholds are NEGATIVE (loss velocity). Only negative velocity
            triggers reductions — positive velocity (gaining) is always fine.
        cooldown_candles: Minimum candles between partial closes.
        min_energy: Only activate when regime energy exceeds this (0-1).
        min_pnl_pct: Minimum unrealized loss (%) to even consider velocity trigger.
            Prevents false triggers on positions with tiny gains fluctuating.
            Default: -0.3% — must be at least slightly underwater.
        acceleration_scale: Max multiplier when acceleration amplifies signal.
            1.0 = ignore acceleration, 1.5 = up to 50% more aggressive.
        tracker_window_candles: Rolling window size for velocity computation.
        tracker_min_samples: Minimum samples before velocity is valid.
    """

    regime_velocity_thresholds: dict[MarketRegime, tuple[tuple[float, float], ...]] = None
    cooldown_candles: int = 10
    min_energy: float = 0.3
    min_pnl_pct: float = -0.3
    acceleration_scale: float = 1.3
    tracker_window_candles: int = 5
    tracker_min_samples: int = 3

    def __post_init__(self) -> None:
        if self.regime_velocity_thresholds is None:
            object.__setattr__(self, "regime_velocity_thresholds", {
                MarketRegime.CRASH: (
                    (-0.5, 0.25),
                    (-1.0, 0.50),
                    (-2.0, 1.0),
                ),
                MarketRegime.VOLATILE_TRENDING: (
                    (-0.3, 0.25),
                    (-0.6, 0.50),
                    (-1.2, 1.0),
                ),
            })

    @property
    def active_regimes(self) -> set[MarketRegime]:
        return set(self.regime_velocity_thresholds.keys())


def _compute_velocity_reduce_fraction(
    velocity: float,
    acceleration: float,
    thresholds: tuple[tuple[float, float], ...],
    acceleration_scale: float = 1.3,
) -> float:
    """Compute position reduction fraction based on P&L velocity. Pure function.

    Velocity thresholds are negative — a velocity of -0.8%/candle means
    the position is losing 0.8% per candle. Returns the reduce_fraction
    from the most severe threshold whose velocity level has been exceeded.

    Acceleration amplifies: if velocity is accelerating (accel < 0),
    scale up the reduce fraction up to acceleration_scale.

    Args:
        velocity: Rate of P&L change (% per candle, negative = losing).
        acceleration: Rate of velocity change (% per candle², negative = accelerating loss).
        thresholds: Escalating (velocity_pct, reduce_fraction) pairs.
        acceleration_scale: Max multiplier for acceleration amplification.

    Returns:
        Fraction of position to reduce (0.0 = none, 1.0 = close all).
    """
    fraction = 0.0
    for threshold_vel, reduce_frac in thresholds:
        if velocity <= threshold_vel:
            fraction = reduce_frac

    # Acceleration amplification: if losses are accelerating, scale up
    if fraction > 0.0 and acceleration < 0.0 and acceleration_scale > 1.0:
        # Scale proportional to how negative acceleration is
        # Clamp so we don't exceed acceleration_scale
        accel_factor = min(1.0 + abs(acceleration) * 0.5, acceleration_scale)
        fraction = min(fraction * accel_factor, 1.0)

    return fraction


class VelocityPositionSizer:
    """Reduces positions when P&L velocity indicates accelerating losses.

    Unlike AdaptivePositionSizer which triggers on absolute P&L level,
    this triggers on the rate of loss. A slow drift to -3% is fine;
    a rapid slide of -0.8%/candle is dangerous regardless of current level.
    """

    def __init__(self, config: VelocitySizerConfig | None = None) -> None:
        self._config = config or VelocitySizerConfig()

    def evaluate(
        self,
        velocity_result: VelocityResult,
        candles_since_last_reduce: int,
        regime: MarketRegime | None = None,
        regime_energy: float = 0.0,
    ) -> float:
        """Evaluate whether to reduce position based on P&L velocity and regime.

        Args:
            velocity_result: Computed velocity from VelocityTracker.
            candles_since_last_reduce: Candles since last partial close.
            regime: Current market regime (from RegimeDetector).
            regime_energy: Regime uncertainty (0=confident, 1=uncertain).

        Returns:
            Fraction of position to reduce (0.0 = no action).
        """
        # Must have enough samples for valid velocity
        if velocity_result.window_size < self._config.tracker_min_samples:
            return 0.0

        # Must be losing money to trigger (guard against noise on flat positions)
        if velocity_result.current_pnl_pct > self._config.min_pnl_pct:
            return 0.0

        # Skip if in cooldown
        if candles_since_last_reduce < self._config.cooldown_candles:
            return 0.0

        # Skip if regime is not in active set
        if regime is None or regime not in self._config.active_regimes:
            return 0.0

        # Skip if regime energy is too low
        if regime_energy < self._config.min_energy:
            return 0.0

        thresholds = self._config.regime_velocity_thresholds.get(regime)
        if thresholds is None:
            return 0.0

        return _compute_velocity_reduce_fraction(
            velocity_result.velocity,
            velocity_result.acceleration,
            thresholds,
            self._config.acceleration_scale,
        )

    @property
    def config(self) -> VelocitySizerConfig:
        return self._config
