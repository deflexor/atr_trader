//! Composite risk scoring — combines regime, velocity, and correlation signals.
//!
//! Pure function: takes (regime, velocity, correlation) → CompositeRiskScore.
//! Weighted combination with synergy bonus when 2+ signals are elevated.

use crate::risk::correlation::CorrelationRiskLevel;
use crate::risk::correlation::CorrelationSignal;
use crate::risk::regime::{MarketRegime, RegimeResult};
use crate::risk::velocity::VelocityResult;

/// Composite risk scoring configuration.
#[derive(Debug, Clone)]
pub struct CompositeRiskConfig {
    pub regime_weight: f64,
    pub velocity_weight: f64,
    pub correlation_weight: f64,
    pub synergy_threshold: f64,
    pub synergy_multiplier: f64,
    pub velocity_max_pct_per_candle: f64,
    pub trailing_tighten_start: f64,
    pub trailing_tighten_scale: f64,
    pub trailing_earlier_activation: f64,
    pub reduce_threshold: f64,
    pub reduce_scale: f64,
}

impl Default for CompositeRiskConfig {
    fn default() -> Self {
        Self {
            regime_weight: 0.35,
            velocity_weight: 0.30,
            correlation_weight: 0.35,
            synergy_threshold: 0.3,
            synergy_multiplier: 1.4,
            velocity_max_pct_per_candle: 2.0,
            trailing_tighten_start: 0.3,
            trailing_tighten_scale: 0.3,
            trailing_earlier_activation: 0.15,
            reduce_threshold: 0.6,
            reduce_scale: 0.5,
        }
    }
}

/// Composite risk assessment result.
#[derive(Debug, Clone)]
pub struct CompositeRiskScore {
    pub score: f64,
    pub regime_sub_score: f64,
    pub velocity_sub_score: f64,
    pub correlation_sub_score: f64,
    pub synergy_active: bool,
    pub trailing_atr_multiplier: Option<f64>,
    pub trailing_activation_reduction: f64,
    pub position_reduce_fraction: f64,
}

/// Compute composite risk score from all risk subsystems. Pure function.
pub fn compute_composite_score(
    regime_result: Option<&RegimeResult>,
    velocity_result: Option<&VelocityResult>,
    correlation_signal: Option<&CorrelationSignal>,
    config: &CompositeRiskConfig,
) -> CompositeRiskScore {
    // Compute normalized sub-scores
    let regime_energy = regime_result.map(|r| r.energy).unwrap_or(0.0);
    let regime_sub = regime_to_score(
        regime_result.map(|r| r.regime),
        regime_energy,
    );
    let velocity_sub = velocity_to_score(velocity_result, config.velocity_max_pct_per_candle);
    let correlation_sub = correlation_to_score(correlation_signal);

    // Weighted linear combination
    let total_weight = config.regime_weight + config.velocity_weight + config.correlation_weight;
    let base_score = (regime_sub * config.regime_weight
        + velocity_sub * config.velocity_weight
        + correlation_sub * config.correlation_weight)
        / total_weight;

    // Synergy bonus: when 2+ sub-scores exceed threshold
    let subs = [regime_sub, velocity_sub, correlation_sub];
    let active_count = subs.iter().filter(|&&s| s >= config.synergy_threshold).count();
    let synergy_active = active_count >= 2;

    let composite = if synergy_active {
        let active_sum: f64 = subs
            .iter()
            .filter(|&&s| s >= config.synergy_threshold)
            .sum();
        let avg_active = active_sum / active_count as f64;
        let bonus = 1.0 + (config.synergy_multiplier - 1.0) * avg_active;
        (base_score * bonus).min(1.0)
    } else {
        base_score
    };

    // Derive trailing stop parameters
    let (trailing_mult, activation_reduction) = if composite >= config.trailing_tighten_start {
        let tighten_range = 1.0 - config.trailing_tighten_start;
        let score_above = (composite - config.trailing_tighten_start) / tighten_range;
        let mult = 1.0 - (1.0 - config.trailing_tighten_scale) * score_above;
        let activation = 1.0 - config.trailing_earlier_activation * score_above;
        (Some(mult), activation)
    } else {
        (None, 1.0)
    };

    // Position reduction
    let reduce_frac = if composite >= config.reduce_threshold {
        let reduce_range = 1.0 - config.reduce_threshold;
        let score_above = (composite - config.reduce_threshold) / reduce_range;
        config.reduce_scale * score_above
    } else {
        0.0
    };

    CompositeRiskScore {
        score: composite,
        regime_sub_score: regime_sub,
        velocity_sub_score: velocity_sub,
        correlation_sub_score: correlation_sub,
        synergy_active,
        trailing_atr_multiplier: trailing_mult,
        trailing_activation_reduction: activation_reduction,
        position_reduce_fraction: reduce_frac,
    }
}

// ─── Sub-Score Normalization ─────────────────────────────────

fn regime_to_score(regime: Option<MarketRegime>, energy: f64) -> f64 {
    let base = match regime {
        Some(MarketRegime::CalmTrending) => 0.0,
        Some(MarketRegime::MeanReverting) => 0.2,
        Some(MarketRegime::VolatileTrending) => 0.6,
        Some(MarketRegime::Crash) => 1.0,
        None => 0.0,
    };
    // Scale by energy — low energy = less certain
    base * (0.5 + 0.5 * energy)
}

fn velocity_to_score(velocity: Option<&VelocityResult>, max_velocity: f64) -> f64 {
    match velocity {
        Some(v) if v.window_size >= 3 => {
            if v.velocity >= 0.0 {
                return 0.0;
            }
            (v.velocity.abs() / max_velocity).min(1.0)
        }
        _ => 0.0,
    }
}

fn correlation_to_score(signal: Option<&CorrelationSignal>) -> f64 {
    match signal {
        Some(s) => match s.risk_level {
            CorrelationRiskLevel::Normal => 0.0,
            CorrelationRiskLevel::Elevated => 0.4,
            CorrelationRiskLevel::High => 0.7,
            CorrelationRiskLevel::Extreme => 1.0,
        },
        None => 0.0,
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn default_config() -> CompositeRiskConfig {
        CompositeRiskConfig::default()
    }

    #[test]
    fn test_composite_no_risk() {
        let result = compute_composite_score(None, None, None, &default_config());
        assert!(result.score < 0.01, "No risk should score near 0");
        assert!(!result.synergy_active);
        assert!(result.position_reduce_fraction < 0.01);
    }

    #[test]
    fn test_regime_sub_score_calm() {
        let score = regime_to_score(Some(MarketRegime::CalmTrending), 0.5);
        assert!((score - 0.0).abs() < 0.01);
    }

    #[test]
    fn test_regime_sub_score_crash() {
        let score = regime_to_score(Some(MarketRegime::Crash), 1.0);
        assert!((score - 1.0).abs() < 0.01);
    }

    #[test]
    fn test_velocity_sub_score_positive() {
        let v = VelocityResult {
            velocity: 0.5,
            acceleration: 0.0,
            window_size: 5,
            current_pnl_pct: 2.0,
        };
        let score = velocity_to_score(Some(&v), 2.0);
        // Positive velocity should score 0
        assert!((score - 0.0).abs() < 0.01);
    }

    #[test]
    fn test_velocity_sub_score_negative() {
        let v = VelocityResult {
            velocity: -1.0,
            acceleration: 0.0,
            window_size: 5,
            current_pnl_pct: -5.0,
        };
        let score = velocity_to_score(Some(&v), 2.0);
        assert!((score - 0.5).abs() < 0.01, "Expected 0.5, got {score}");
    }

    #[test]
    fn test_correlation_sub_score_normal() {
        let s = CorrelationSignal {
            risk_level: CorrelationRiskLevel::Normal,
            eth_return_pct: 0.0,
            btc_return_pct: 0.0,
            divergence_pct: 0.0,
            trailing_atr_multiplier: None,
            position_reduce_fraction: 0.0,
        };
        let score = correlation_to_score(Some(&s));
        assert!((score - 0.0).abs() < 0.01);
    }

    #[test]
    fn test_correlation_sub_score_extreme() {
        let s = CorrelationSignal {
            risk_level: CorrelationRiskLevel::Extreme,
            eth_return_pct: -5.0,
            btc_return_pct: 0.0,
            divergence_pct: -5.0,
            trailing_atr_multiplier: Some(0.25),
            position_reduce_fraction: 0.25,
        };
        let score = correlation_to_score(Some(&s));
        assert!((score - 1.0).abs() < 0.01);
    }

    #[test]
    fn test_composite_with_regime_and_correlation() {
        let regime = RegimeResult {
            regime: MarketRegime::Crash,
            confidence: 0.8,
            volatility_percentile: 0.9,
            skewness: -2.0,
            energy: 0.6,
        };

        let correlation = CorrelationSignal {
            risk_level: CorrelationRiskLevel::High,
            eth_return_pct: -2.0,
            btc_return_pct: 0.0,
            divergence_pct: -2.0,
            trailing_atr_multiplier: Some(0.5),
            position_reduce_fraction: 0.0,
        };

        let result = compute_composite_score(
            Some(&regime),
            None,
            Some(&correlation),
            &default_config(),
        );

        // Two elevated signals → synergy bonus should be active
        assert!(result.score > 0.5, "Score should be elevated, got {}", result.score);
        assert!(result.synergy_active, "Synergy should be active");
        // Trailing should be tightening
        assert!(result.trailing_atr_multiplier.is_some(), "Trailing should be tightening");
        // Position reduction may be active
        assert!(result.position_reduce_fraction >= 0.0);
    }
}
