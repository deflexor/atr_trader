use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum SignalDirection {
    Long,
    Short,
    Neutral,
}

impl SignalDirection {
    pub fn is_actionable(&self) -> bool {
        matches!(self, SignalDirection::Long | SignalDirection::Short)
    }

    pub fn is_long(&self) -> bool {
        matches!(self, SignalDirection::Long)
    }

    pub fn is_short(&self) -> bool {
        matches!(self, SignalDirection::Short)
    }
}

#[derive(Debug, Clone)]
pub struct Signal {
    pub symbol: String,
    pub direction: SignalDirection,
    pub strength: f64,
    pub confidence: f64,
    pub price: f64,
    pub timestamp: i64,
    pub strategy_id: String,
    pub sources: Vec<String>,
    pub features: Option<HashMap<String, f64>>,
}

impl Signal {
    pub fn neutral(symbol: &str) -> Self {
        Self {
            symbol: symbol.to_string(),
            direction: SignalDirection::Neutral,
            strength: 0.0,
            confidence: 0.0,
            price: 0.0,
            timestamp: 0,
            strategy_id: String::new(),
            sources: Vec::new(),
            features: None,
        }
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_signal_direction() {
        assert!(SignalDirection::Long.is_actionable());
        assert!(SignalDirection::Short.is_actionable());
        assert!(!SignalDirection::Neutral.is_actionable());
        assert!(SignalDirection::Long.is_long());
        assert!(SignalDirection::Short.is_short());
    }

    #[test]
    fn test_neutral_signal() {
        let s = Signal::neutral("SOLUSDC");
        assert_eq!(s.symbol, "SOLUSDC");
        assert_eq!(s.direction, SignalDirection::Neutral);
        assert!((s.strength - 0.0).abs() < f64::EPSILON);
    }
}
