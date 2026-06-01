//! Funding Rate Arbitrage Strategy
//!
//! The strategy is simple:
//! 1. For each symbol, check the current funding rate on the perp
//! 2. If the rate exceeds a threshold (e.g., 0.005% per 8h), enter:
//!    - Long 1 unit on spot
//!    - Short 1 unit on perpetual
//! 3. Hold for N periods, collecting funding every 8h
//! 4. Exit when funding drops below threshold/2
//!
//! The position is delta-neutral: market moves cancel out.
//! Return comes purely from collecting funding payments.

use serde::{Deserialize, Serialize};

/// Configuration for the funding rate arb strategy.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FundingArbConfig {
    /// Minimum funding rate to enter (per 8h, e.g., 0.00005 = 0.005%)
    pub min_rate_threshold: f64,
    /// Maximum number of simultaneous carry positions
    pub max_positions: usize,
    /// Transaction cost per leg (bps)
    pub cost_bps: f64,
    /// Rebalance interval in days
    pub rebalance_days: usize,
    /// Lookback days for rate averaging (use mean rate over window)
    pub rate_lookback: usize,
    /// If true, only enter when funding rate is positive (long pays short)
    pub only_positive_funding: bool,
    /// Annualized return target for position sizing
    pub target_apr: f64,
    /// Capital allocation to funding arb (%)
    pub capital_fraction: f64,
}

impl Default for FundingArbConfig {
    fn default() -> Self {
        Self {
            // 0.005% per 8h ≈ 0.45% per month ≈ 5.4% APR — typical baseline
            min_rate_threshold: 0.00005,
            max_positions: 5,
            cost_bps: 10.0,
            rebalance_days: 7,
            rate_lookback: 3,
            only_positive_funding: true,
            target_apr: 0.15, // 15% target
            capital_fraction: 1.0,
        }
    }
}

/// A single funding rate carry position.
#[derive(Debug, Clone)]
pub struct FundingArbPosition {
    pub symbol: String,
    /// Entry funding rate (per 8h)
    pub entry_rate: f64,
    /// Timestamp when position was opened
    pub open_time: i64,
    /// Accumulated funding collected
    pub collected_funding: f64,
    /// Notional size in quote currency
    pub notional: f64,
    /// Number of 8h funding periods held
    pub periods_held: usize,
    /// Current status
    pub status: PositionStatus,
}

#[derive(Debug, Clone, PartialEq)]
pub enum PositionStatus {
    Active,
    Closed,
}

impl FundingArbPosition {
    pub fn new(symbol: &str, entry_rate: f64, notional: f64, open_time: i64) -> Self {
        Self {
            symbol: symbol.to_string(),
            entry_rate,
            open_time,
            collected_funding: 0.0,
            notional,
            periods_held: 0,
            status: PositionStatus::Active,
        }
    }

    /// Returns the net P&L from funding collected minus transaction costs.
    pub fn net_pnl(&self, cost_bps: f64) -> f64 {
        let tx_cost = self.notional * (cost_bps / 10000.0) * 2.0; // entry + exit
        self.collected_funding - tx_cost
    }

    /// Return percentage.
    pub fn return_pct(&self, cost_bps: f64) -> f64 {
        if self.notional <= 0.0 {
            return 0.0;
        }
        (self.net_pnl(cost_bps) / self.notional) * 100.0
    }
}

/// Collect funding for a period. Rate is the funding rate for this period.
pub fn accrue_funding(pos: &mut FundingArbPosition, rate: f64) {
    let payment = pos.notional * rate;
    pos.collected_funding += payment;
    pos.periods_held += 1;
}

/// Decide whether to enter a carry position.
pub fn should_enter(
    current_rate: f64,
    config: &FundingArbConfig,
    active_positions: usize,
) -> bool {
    if active_positions >= config.max_positions {
        return false;
    }
    if config.only_positive_funding && current_rate <= 0.0 {
        return false;
    }
    current_rate >= config.min_rate_threshold
}

/// Decide whether to exit a carry position.
pub fn should_exit(
    pos: &FundingArbPosition,
    current_rate: f64,
    config: &FundingArbConfig,
    current_time: i64,
) -> bool {
    // Exit if funding rate has dropped below half the threshold
    // (meaning the carry trade is no longer attractive)
    if current_rate < config.min_rate_threshold * 0.5 {
        return true;
    }
    // Exit if funding turned negative (now YOU pay)
    if config.only_positive_funding && current_rate <= 0.0 {
        return true;
    }
    // Exit if held too long (rebalance)
    let hours_held = (current_time - pos.open_time) / 3600;
    if hours_held >= config.rebalance_days as i64 * 24 {
        return true;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_should_enter_basic() {
        let config = FundingArbConfig::default();
        assert!(should_enter(0.0001, &config, 0)); // 0.01% > 0.005%
        assert!(!should_enter(0.00002, &config, 0)); // 0.002% < 0.005%
        assert!(!should_enter(0.0001, &config, 5)); // max positions reached
        assert!(!should_enter(-0.0001, &config, 0)); // negative rate
    }

    #[test]
    fn test_should_exit_basic() {
        let config = FundingArbConfig::default();
        let mut pos = FundingArbPosition::new("BTCUSDC", 0.0001, 1000.0, 1000000);
        pos.periods_held = 10;

        // Rate dropped below threshold
        assert!(should_exit(&pos, 0.00001, &config, 1000000));

        // Rate still good
        assert!(!should_exit(&pos, 0.00008, &config, 1000000));

        // Negative rate
        assert!(should_exit(&pos, -0.00001, &config, 1000000));
    }

    #[test]
    fn test_accrue_funding() {
        let mut pos = FundingArbPosition::new("BTCUSDC", 0.0001, 10000.0, 1000000);
        accrue_funding(&mut pos, 0.0001);
        assert_eq!(pos.periods_held, 1);
        assert!((pos.collected_funding - 1.0).abs() < 0.001); // 10000 * 0.0001 = 1.0
    }

    #[test]
    fn test_net_pnl() {
        let mut pos = FundingArbPosition::new("BTCUSDC", 0.0001, 10000.0, 1000000);
        accrue_funding(&mut pos, 0.0001);
        accrue_funding(&mut pos, 0.0001);
        accrue_funding(&mut pos, 0.0001);
        // Collected: 10000 * 0.0001 * 3 = $3.00
        // Cost: 10000 * 10bps * 2 = $20.00
        // Net: $3 - $20 = -$17
        let pnl = pos.net_pnl(10.0);
        assert!((pnl - (-17.0)).abs() < 0.01);
    }

    #[test]
    fn test_long_hold_no_exit() {
        let config = FundingArbConfig::default();
        let mut pos = FundingArbPosition::new("SOLUSDC", 0.00005, 5000.0, 1000000);
        // Over 7 days = 21 funding periods, good rate
        for _ in 0..21 {
            accrue_funding(&mut pos, 0.00006);
        }
        // Before 7 day rebalance boundary: rate still above half-threshold
        assert!(!should_exit(&pos, 0.00003, &config, 1000000 + 6 * 86400 + 86399));
        // At exactly 7 days: should exit due to rebalance
        assert!(should_exit(&pos, 0.00003, &config, 1000000 + 7 * 86400));
    }
}
