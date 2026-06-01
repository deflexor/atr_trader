//! Signal fusion — combines sub-signals into a single trading signal.
//!
//! Logic:
//! 1. Collect non-neutral sub-signals
//! 2. If both LONG and SHORT → conflict (cancel) → NEUTRAL
//! 3. Strength = max(strengths) × synergy bonus
//!    synergy = 1.0 + 0.2 × (n_agreeing - 1)
//! 4. Confidence = n_agreeing / total_possible_signals
//! 5. News sentiment is built into the sub-signals already

use crate::types::signal::{Signal, SignalDirection};

use super::tech::SubSignal;

/// Total number of possible sub-signal types.
/// Used to compute confidence fraction.
pub const TOTAL_SIGNAL_TYPES: usize = 6; // breakout, mean_reversion, trend, vwap, divergence, news_sentiment

/// Fuse multiple sub-signals into one final Signal.
///
/// Returns NEUTRAL if no active signals or if there's a LONG↔SHORT conflict.
pub fn fuse_signals(
    subsignals: &[SubSignal],
    symbol: &str,
    price: f64,
    timestamp: i64,
) -> Signal {
    let active: Vec<&SubSignal> = subsignals
        .iter()
        .filter(|s| s.direction.is_actionable())
        .collect();

    if active.is_empty() {
        return Signal::neutral(symbol);
    }

    // Weighted majority vote instead of unanimous agreement.
    // Each signal's strength is its vote weight.
    // The direction with the most total weight wins.
    let long_weight: f64 = active
        .iter()
        .filter(|s| s.direction.is_long())
        .map(|s| s.strength)
        .sum();
    let short_weight: f64 = active
        .iter()
        .filter(|s| s.direction.is_short())
        .map(|s| s.strength)
        .sum();

    // Minimum weight threshold — need at least this much signal to act
    const MIN_VOTE_WEIGHT: f64 = 0.3;

    let (direction, agreeing, final_weight) = if long_weight > short_weight && long_weight >= MIN_VOTE_WEIGHT {
        let agree: Vec<&&SubSignal> = active.iter().filter(|s| s.direction.is_long()).collect();
        (SignalDirection::Long, agree, long_weight)
    } else if short_weight > long_weight && short_weight >= MIN_VOTE_WEIGHT {
        let agree: Vec<&&SubSignal> = active.iter().filter(|s| s.direction.is_short()).collect();
        (SignalDirection::Short, agree, short_weight)
    } else {
        // Tie or below threshold → neutral
        return Signal::neutral(symbol);
    };

    // Strength: the vote weight, capped at 1.0
    let final_strength = final_weight.min(1.0);

    // Confidence: fraction of total possible signals that the WINNING side represents
    let confidence = final_weight / 6.0; // 6 = total possible signal types

    // Collect source names (all active, not just winners)
    // This gives visibility into what signals were considered
    let sources: Vec<String> = active.iter().map(|s| s.source.to_string()).collect();

    Signal {
        symbol: symbol.to_string(),
        direction,
        strength: final_strength,
        confidence,
        price,
        timestamp,
        strategy_id: "enhanced".to_string(),
        sources,
        features: None,
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::signal::SignalDirection;

    #[test]
    fn test_fusion_no_signals() {
        let signal = fuse_signals(&[], "SOLUSDC", 100.0, 0);
        assert_eq!(signal.direction, SignalDirection::Neutral);
    }

    #[test]
    fn test_fusion_single_long() {
        let subs = vec![SubSignal {
            direction: SignalDirection::Long,
            strength: 0.6,
            source: "breakout",
        }];
        let signal = fuse_signals(&subs, "SOLUSDC", 100.0, 0);
        assert_eq!(signal.direction, SignalDirection::Long);
        assert!((signal.strength - 0.6).abs() < 0.01);
    }

    #[test]
    fn test_fusion_weighted_vote() {
        let subs = vec![
            SubSignal {
                direction: SignalDirection::Long,
                strength: 0.6,
                source: "breakout",
            },
            SubSignal {
                direction: SignalDirection::Long,
                strength: 0.5,
                source: "trend",
            },
            SubSignal {
                direction: SignalDirection::Long,
                strength: 0.4,
                source: "vwap",
            },
        ];
        let signal = fuse_signals(&subs, "SOLUSDC", 100.0, 0);
        assert_eq!(signal.direction, SignalDirection::Long);
        // Weighted strength = min(0.6+0.5+0.4, 1.0) = 1.0
        assert!((signal.strength - 1.0).abs() < 0.01);
    }

    #[test]
    fn test_fusion_weighted_majority_short() {
        let subs = vec![
            SubSignal {
                direction: SignalDirection::Long,
                strength: 0.3,
                source: "breakout",
            },
            SubSignal {
                direction: SignalDirection::Short,
                strength: 0.7,
                source: "trend",
            },
        ];
        let signal = fuse_signals(&subs, "SOLUSDC", 100.0, 0);
        assert_eq!(
            signal.direction,
            SignalDirection::Short,
            "Short 0.7 > Long 0.3 → Short should win"
        );
        assert!((signal.strength - 0.7).abs() < 0.01);
    }

    #[test]
    fn test_fusion_tie_below_threshold() {
        let subs = vec![
            SubSignal {
                direction: SignalDirection::Long,
                strength: 0.15,
                source: "breakout",
            },
            SubSignal {
                direction: SignalDirection::Short,
                strength: 0.15,
                source: "trend",
            },
        ];
        let signal = fuse_signals(&subs, "SOLUSDC", 100.0, 0);
        assert_eq!(
            signal.direction,
            SignalDirection::Neutral,
            "Both below threshold → Neutral"
        );
    }

    #[test]
    fn test_fusion_strength_capped() {
        let subs = vec![
            SubSignal { direction: SignalDirection::Long, strength: 0.9, source: "breakout" },
            SubSignal { direction: SignalDirection::Long, strength: 0.9, source: "trend" },
            SubSignal { direction: SignalDirection::Long, strength: 0.9, source: "vwap" },
            SubSignal { direction: SignalDirection::Long, strength: 0.9, source: "divergence" },
        ];
        let signal = fuse_signals(&subs, "SOLUSDC", 100.0, 0);
        assert!(signal.strength <= 1.0, "Strength should be capped at 1.0");
    }
}
