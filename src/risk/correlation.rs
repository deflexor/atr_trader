//! ETH/BTC correlation monitor for leading risk signals.
//!
//! Tracks ETH price alongside BTC to detect divergences where ETH drops
//! while BTC is flat — a leading indicator that BTC may follow.
//!
//! Key insight: ETH dropping before BTC is a LEADING signal that can
//! trigger pre-emptive trailing stop tightening BEFORE the BTC drop.

use std::collections::VecDeque;

/// Risk level from correlation monitoring.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum CorrelationRiskLevel {
    Normal,
    Elevated,
    High,
    Extreme,
}

/// Correlation monitoring result.
#[derive(Debug, Clone)]
pub struct CorrelationSignal {
    pub risk_level: CorrelationRiskLevel,
    pub eth_return_pct: f64,
    pub btc_return_pct: f64,
    pub divergence_pct: f64,
    pub trailing_atr_multiplier: Option<f64>,
    pub position_reduce_fraction: f64,
}

/// Configuration for correlation monitoring.
#[derive(Debug, Clone)]
pub struct CorrelationConfig {
    pub lookback_candles: usize,
    pub mild_divergence_pct: f64,
    pub strong_divergence_pct: f64,
    pub extreme_divergence_pct: f64,
    pub btc_flat_threshold_pct: f64,
    pub trailing_tighten_elevated: f64,
    pub trailing_tighten_high: f64,
    pub trailing_tighten_extreme: f64,
    pub reduce_at_extreme: f64,
    pub min_samples: usize,
}

impl Default for CorrelationConfig {
    fn default() -> Self {
        Self {
            lookback_candles: 20,
            mild_divergence_pct: -0.8,
            strong_divergence_pct: -1.6,
            extreme_divergence_pct: -2.5,
            btc_flat_threshold_pct: 0.3,
            trailing_tighten_elevated: 0.75,
            trailing_tighten_high: 0.50,
            trailing_tighten_extreme: 0.25,
            reduce_at_extreme: 0.25,
            min_samples: 5,
        }
    }
}

/// Stateful correlation monitor.
pub struct CorrelationMonitor {
    config: CorrelationConfig,
    btc_prices: VecDeque<f64>,
    eth_prices: VecDeque<f64>,
}

impl CorrelationMonitor {
    pub fn new(config: CorrelationConfig) -> Self {
        let lookback = config.lookback_candles + 1;
        Self {
            config,
            btc_prices: VecDeque::with_capacity(lookback),
            eth_prices: VecDeque::with_capacity(lookback),
        }
    }

    pub fn update_btc(&mut self, close_price: f64) {
        self.btc_prices.push_back(close_price);
        if self.btc_prices.len() > self.config.lookback_candles + 1 {
            self.btc_prices.pop_front();
        }
    }

    pub fn update_eth(&mut self, close_price: f64) {
        self.eth_prices.push_back(close_price);
        if self.eth_prices.len() > self.config.lookback_candles + 1 {
            self.eth_prices.pop_front();
        }
    }

    /// Evaluate the current correlation signal.
    pub fn evaluate(&self) -> CorrelationSignal {
        let eth_return = compute_return(&self.eth_prices);
        let btc_return = compute_return(&self.btc_prices);
        let divergence = eth_return - btc_return;

        let risk_level = classify_divergence(
            divergence,
            btc_return,
            self.config.btc_flat_threshold_pct,
            self.config.mild_divergence_pct,
            self.config.strong_divergence_pct,
            self.config.extreme_divergence_pct,
        );

        let (trailing_mult, reduce_frac) = match risk_level {
            CorrelationRiskLevel::Elevated => {
                (Some(self.config.trailing_tighten_elevated), 0.0)
            }
            CorrelationRiskLevel::High => {
                (Some(self.config.trailing_tighten_high), 0.0)
            }
            CorrelationRiskLevel::Extreme => {
                (Some(self.config.trailing_tighten_extreme), self.config.reduce_at_extreme)
            }
            CorrelationRiskLevel::Normal => (None, 0.0),
        };

        CorrelationSignal {
            risk_level,
            eth_return_pct: eth_return,
            btc_return_pct: btc_return,
            divergence_pct: divergence,
            trailing_atr_multiplier: trailing_mult,
            position_reduce_fraction: reduce_frac,
        }
    }

    pub fn reset(&mut self) {
        self.btc_prices.clear();
        self.eth_prices.clear();
    }

    pub fn has_sufficient_data(&self) -> bool {
        self.eth_prices.len() >= self.config.min_samples
            && self.btc_prices.len() >= self.config.min_samples
    }
}

// ─── Pure Functions ─────────────────────────────────────────

/// Compute % return from oldest to newest price. Pure function.
fn compute_return(prices: &VecDeque<f64>) -> f64 {
    if prices.len() < 2 {
        return 0.0;
    }
    let oldest = prices[0];
    let newest = prices[prices.len() - 1];
    if oldest == 0.0 {
        return 0.0;
    }
    ((newest - oldest) / oldest) * 100.0
}

/// Classify divergence risk level. Pure function.
fn classify_divergence(
    divergence_pct: f64,
    btc_return_pct: f64,
    btc_flat_threshold: f64,
    mild: f64,
    strong: f64,
    extreme: f64,
) -> CorrelationRiskLevel {
    // If BTC is also dropping, the divergence is less predictive
    let btc_dropping = btc_return_pct < -btc_flat_threshold;

    if divergence_pct <= extreme && !btc_dropping {
        return CorrelationRiskLevel::Extreme;
    }
    if divergence_pct <= strong && !btc_dropping {
        return CorrelationRiskLevel::High;
    }
    if divergence_pct <= mild && !btc_dropping {
        return CorrelationRiskLevel::Elevated;
    }
    CorrelationRiskLevel::Normal
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compute_return_positive() {
        let mut prices = VecDeque::new();
        prices.push_back(100.0);
        prices.push_back(110.0);
        let ret = compute_return(&prices);
        assert!((ret - 10.0).abs() < 0.01);
    }

    #[test]
    fn test_compute_return_negative() {
        let mut prices = VecDeque::new();
        prices.push_back(100.0);
        prices.push_back(90.0);
        let ret = compute_return(&prices);
        assert!((ret - (-10.0)).abs() < 0.01);
    }

    #[test]
    fn test_compute_return_insufficient() {
        let mut prices = VecDeque::new();
        prices.push_back(100.0);
        assert!((compute_return(&prices) - 0.0).abs() < 0.01);
    }

    #[test]
    fn test_classify_normal() {
        let level = classify_divergence(-0.5, 0.0, 0.3, -0.8, -1.6, -2.5);
        assert_eq!(level, CorrelationRiskLevel::Normal);
    }

    #[test]
    fn test_classify_elevated() {
        let level = classify_divergence(-1.0, 0.1, 0.3, -0.8, -1.6, -2.5);
        assert_eq!(level, CorrelationRiskLevel::Elevated);
    }

    #[test]
    fn test_classify_extreme() {
        let level = classify_divergence(-3.0, 0.0, 0.3, -0.8, -1.6, -2.5);
        assert_eq!(level, CorrelationRiskLevel::Extreme);
    }

    #[test]
    fn test_classify_btc_dropping_no_divergence() {
        // If BTC is also dropping hard, the divergence isn't as predictive
        let level = classify_divergence(-2.0, -2.0, 0.3, -0.8, -1.6, -2.5);
        assert_eq!(
            level,
            CorrelationRiskLevel::Normal,
            "BTC dropping should suppress divergence signal"
        );
    }

    #[test]
    fn test_monitor_evaluate() {
        let config = CorrelationConfig::default();
        let mut monitor = CorrelationMonitor::new(config);
        // Both BTC and ETH moving up together
        for i in 0..10 {
            monitor.update_btc(100.0 + i as f64);
            monitor.update_eth(2000.0 + i as f64 * 20.0);
        }
        let signal = monitor.evaluate();
        // Both moving up → should be NORMAL
        assert_eq!(signal.risk_level, CorrelationRiskLevel::Normal);
    }

    #[test]
    fn test_monitor_elevated() {
        let config = CorrelationConfig::default();
        let mut monitor = CorrelationMonitor::new(config);
        // BTC flat, ETH dropping
        for i in 0..10 {
            monitor.update_btc(100.0);
            monitor.update_eth(2000.0 - i as f64 * 30.0);
        }
        let signal = monitor.evaluate();
        // ETH dropped significantly while BTC flat → should trigger
        assert!(
            signal.risk_level == CorrelationRiskLevel::Elevated
                || signal.risk_level == CorrelationRiskLevel::High
                || signal.risk_level == CorrelationRiskLevel::Extreme,
            "Should detect divergence, got {:?}",
            signal.risk_level
        );
    }

    #[test]
    fn test_has_sufficient_data() {
        let config = CorrelationConfig::default();
        let monitor = CorrelationMonitor::new(config);
        assert!(!monitor.has_sufficient_data());

        let config = CorrelationConfig::default();
        let mut monitor = CorrelationMonitor::new(config);
        for i in 0..10 {
            monitor.update_btc(100.0 + i as f64);
            monitor.update_eth(2000.0 + i as f64);
        }
        assert!(monitor.has_sufficient_data());
    }
}
