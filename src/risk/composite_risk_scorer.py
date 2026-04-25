"""Composite risk scorer combining regime, velocity, and correlation signals.

When multiple risk signals align, the combined signal is stronger than any
individual one. Currently each sizer acts independently — the composite score
unlocks signal synergy.

Key insight from Phases 1-4: "The problem is detection, not action."
Trailing stops are the real DD control. The composite scorer's PRIMARY output
is recommended trailing stop parameters. Position reduction is SECONDARY.

Design:
    - Pure function: takes (regime, velocity, correlation) → CompositeRiskScore
    - No state, no side effects — completely testable
    - Weighted combination of normalized sub-scores
    - Synergy bonus when 2+ signals are elevated simultaneously
    - Output: 0-1 composite score + trailing params + reduce fraction

Sub-score normalization:
    Regime:     CRASH=1.0, VOLATILE=0.6, MEAN_REVERTING=0.2, CALM=0.0
    Velocity:   |velocity| / max_velocity (capped at 1.0)
    Correlation: NORMAL=0.0, ELEVATED=0.4, HIGH=0.7, EXTREME=1.0

Synergy bonus:
    When 2+ sub-scores exceed 0.3, apply a multiplicative bonus (1.0-1.5x).
    This captures the insight that aligned signals are more predictive than
    any single signal alone. ETH divergence + CRASH regime = much more
    dangerous than either signal in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .correlation_monitor import CorrelationRiskLevel, CorrelationSignal
from .regime_detector import MarketRegime, RegimeResult
from .velocity_tracker import VelocityResult


@dataclass(frozen=True)
class CompositeRiskConfig:
    """Immutable composite risk scoring configuration.

    Attributes:
        regime_weight: Weight of regime signal in composite score (0-1).
        velocity_weight: Weight of velocity signal in composite score (0-1).
        correlation_weight: Weight of correlation signal in composite score (0-1).
        synergy_threshold: Minimum sub-score to count as "active" for synergy bonus.
        synergy_multiplier: Max bonus when all 3 signals are active (1.0 = no bonus).
        velocity_max_pct_per_candle: Velocity at which sub-score reaches 1.0.
        trailing_tighten_start: Composite score above which trailing begins tightening.
        trailing_tighten_scale: Max trailing multiplier at composite_score=1.0 (lower = tighter).
        trailing_earlier_activation: Reduce activation ATR by this factor per 0.1 of score above threshold.
        reduce_threshold: Composite score above which position reduction activates.
        reduce_scale: Max position reduction fraction at composite_score=1.0.
    """

    regime_weight: float = 0.35
    velocity_weight: float = 0.30
    correlation_weight: float = 0.35
    synergy_threshold: float = 0.3
    synergy_multiplier: float = 1.4
    velocity_max_pct_per_candle: float = 2.0
    trailing_tighten_start: float = 0.3
    trailing_tighten_scale: float = 0.3  # At score=1.0, trailing = 0.3x (70% tighter)
    trailing_earlier_activation: float = 0.15  # Per 0.1 of score above threshold
    reduce_threshold: float = 0.6
    reduce_scale: float = 0.5  # At score=1.0, reduce 50%


@dataclass(frozen=True)
class CompositeRiskScore:
    """Immutable composite risk assessment.

    Attributes:
        score: Overall risk score 0-1 (0=safe, 1=extreme danger).
        regime_sub_score: Normalized regime contribution (0-1).
        velocity_sub_score: Normalized velocity contribution (0-1).
        correlation_sub_score: Normalized correlation contribution (0-1).
        synergy_active: Whether synergy bonus was applied.
        trailing_atr_multiplier: Recommended trailing multiplier (None = no change).
        trailing_activation_reduction: Factor to reduce activation ATR by (1.0 = no change, <1 = activate earlier).
        position_reduce_fraction: Fraction of position to reduce (0.0 = none).
    """

    score: float
    regime_sub_score: float
    velocity_sub_score: float
    correlation_sub_score: float
    synergy_active: bool
    trailing_atr_multiplier: float | None
    trailing_activation_reduction: float
    position_reduce_fraction: float


def _regime_to_score(regime: MarketRegime | None, energy: float) -> float:
    """Convert regime classification to a 0-1 risk sub-score. Pure function."""
    if regime is None:
        return 0.0
    base = {
        MarketRegime.CALM_TRENDING: 0.0,
        MarketRegime.MEAN_REVERTING: 0.2,
        MarketRegime.VOLATILE_TRENDING: 0.6,
        MarketRegime.CRASH: 1.0,
    }.get(regime, 0.0)
    # Scale by energy — low energy regime classifications are less certain
    return base * (0.5 + 0.5 * energy)


def _velocity_to_score(velocity_result: VelocityResult | None, max_velocity: float) -> float:
    """Convert velocity to a 0-1 risk sub-score. Pure function."""
    if velocity_result is None or velocity_result.window_size < 3:
        return 0.0
    # Only negative velocity (losing money) contributes to risk
    if velocity_result.velocity >= 0.0:
        return 0.0
    # Normalize: |velocity| / max_velocity, capped at 1.0
    return min(abs(velocity_result.velocity) / max_velocity, 1.0)


def _correlation_to_score(signal: CorrelationSignal | None) -> float:
    """Convert correlation signal to a 0-1 risk sub-score. Pure function."""
    if signal is None:
        return 0.0
    return {
        CorrelationRiskLevel.NORMAL: 0.0,
        CorrelationRiskLevel.ELEVATED: 0.4,
        CorrelationRiskLevel.HIGH: 0.7,
        CorrelationRiskLevel.EXTREME: 1.0,
    }.get(signal.risk_level, 0.0)


def compute_composite_score(
    regime_result: RegimeResult | None,
    velocity_result: VelocityResult | None,
    correlation_signal: CorrelationSignal | None,
    config: CompositeRiskConfig | None = None,
) -> CompositeRiskScore:
    """Compute composite risk score from all risk subsystems. Pure function.

    Combines regime, velocity, and correlation signals into a unified 0-1 score
    with synergy bonus when multiple signals align. Derives trailing stop
    parameters and position reduction fraction from the composite score.

    Args:
        regime_result: Latest regime detection result (or None).
        velocity_result: Latest velocity computation result (or None).
        correlation_signal: Latest correlation monitoring signal (or None).
        config: Scoring configuration (uses defaults if None).

    Returns:
        CompositeRiskScore with overall score and recommended actions.
    """
    cfg = config or CompositeRiskConfig()

    # Compute normalized sub-scores
    regime_energy = regime_result.energy if regime_result else 0.0
    regime_sub = _regime_to_score(
        regime_result.regime if regime_result else None, regime_energy,
    )
    velocity_sub = _velocity_to_score(velocity_result, cfg.velocity_max_pct_per_candle)
    correlation_sub = _correlation_to_score(correlation_signal)

    # Weighted linear combination
    total_weight = cfg.regime_weight + cfg.velocity_weight + cfg.correlation_weight
    base_score = (
        regime_sub * cfg.regime_weight
        + velocity_sub * cfg.velocity_weight
        + correlation_sub * cfg.correlation_weight
    ) / total_weight

    # Synergy bonus: when 2+ sub-scores exceed threshold
    active_count = sum(1 for s in (regime_sub, velocity_sub, correlation_sub) if s >= cfg.synergy_threshold)
    synergy_active = active_count >= 2
    if synergy_active:
        # Bonus scales with how many signals are active and their intensity
        avg_active = sum(s for s in (regime_sub, velocity_sub, correlation_sub) if s >= cfg.synergy_threshold) / active_count
        bonus = 1.0 + (cfg.synergy_multiplier - 1.0) * avg_active
        composite = min(base_score * bonus, 1.0)
    else:
        composite = base_score

    # Derive trailing stop parameters from composite score
    if composite >= cfg.trailing_tighten_start:
        # Linear interpolation: at threshold → 1.0 (no tightening), at 1.0 → tighten_scale
        tighten_range = 1.0 - cfg.trailing_tighten_start
        score_above = (composite - cfg.trailing_tighten_start) / tighten_range
        trailing_mult = 1.0 - (1.0 - cfg.trailing_tighten_scale) * score_above
        # Earlier activation: reduce activation ATR
        activation_reduction = 1.0 - cfg.trailing_earlier_activation * score_above
    else:
        trailing_mult = None
        activation_reduction = 1.0

    # Derive position reduction from composite score
    if composite >= cfg.reduce_threshold:
        reduce_range = 1.0 - cfg.reduce_threshold
        score_above = (composite - cfg.reduce_threshold) / reduce_range
        reduce_frac = cfg.reduce_scale * score_above
    else:
        reduce_frac = 0.0

    return CompositeRiskScore(
        score=composite,
        regime_sub_score=regime_sub,
        velocity_sub_score=velocity_sub,
        correlation_sub_score=correlation_sub,
        synergy_active=synergy_active,
        trailing_atr_multiplier=trailing_mult,
        trailing_activation_reduction=activation_reduction,
        position_reduce_fraction=reduce_frac,
    )
