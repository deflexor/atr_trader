//! Funding Rate Arbitrage Backtest Engine
//!
//! Simulates a cross-sectional funding rate arbitrage strategy:
//! long 1 unit spot / short 1 unit perp → collects funding, delta-neutral.
//!
//! Accounting:
//!   - Capital is always the total cash + position equity
//!   - Entry doesn't change capital (spot purchase funded by perp proceeds)
//!   - Each 8h: capital += notional × funding_rate
//!   - Exit: capital -= costs (entry + exit for both legs)

use std::collections::HashMap;
use crate::strategy::funding_arb::{FundingArbConfig, should_enter};

/// Result of a funding arb backtest
#[derive(Debug, Clone)]
pub struct FundingArbResult {
    pub initial_capital: f64,
    pub final_capital: f64,
    pub total_return_pct: f64,
    pub max_drawdown_pct: f64,
    pub sharpe_ratio: f64,
    pub total_trades: usize,
    pub total_carry_collected: f64,
    pub total_tx_costs: f64,
    pub duration_days: i64,
    pub rebalances: usize,
}

/// Run a cross-sectional funding rate arb backtest.
///
/// `funding_history`: Map of symbol → Vec of (timestamp, rate) ordered by time
pub fn backtest_funding_arb(
    funding_history: &HashMap<String, Vec<(i64, f64)>>,
    capital: f64,
    config: &FundingArbConfig,
) -> FundingArbResult {
    if funding_history.is_empty() {
        return empty_result(capital);
    }

    // Build unified timeline across all symbols
    let mut all_timestamps: Vec<i64> = funding_history
        .values()
        .flat_map(|rates| rates.iter().map(|(ts, _)| *ts))
        .collect();
    all_timestamps.sort_unstable();
    all_timestamps.dedup();
    if all_timestamps.is_empty() {
        return empty_result(capital);
    }

    let start_ts = all_timestamps[0];
    let end_ts = all_timestamps[all_timestamps.len() - 1];
    let duration_days = (end_ts - start_ts) / 86400;

    let mut cash = capital;
    let mut peak = capital;
    let mut max_dd = 0.0;
    let mut total_carry = 0.0;
    let mut total_costs = 0.0;
    let mut total_trades = 0;
    let mut rebalances = 0;

    // Active positions: symbol → (entry_ts, notional, accrued_carry)
    let mut positions: HashMap<String, (i64, f64, f64)> = HashMap::new();
    let mut last_rebalance = start_ts;

    for &ts in &all_timestamps {
        let is_rebalance = ts == start_ts
            || (ts - last_rebalance) >= (config.rebalance_days as i64) * 86400;

        if is_rebalance && (ts != start_ts || positions.is_empty()) {
            rebalances += 1;

            // Close existing positions (pay exit costs only — notional stays in capital)
            for v in positions.values() {
                let (_entry_ts, notional, accrued) = *v;
                let exit_cost = notional * (config.cost_bps / 10000.0) * 2.0; // close both legs
                cash += accrued - exit_cost;
                total_carry += accrued;
                total_costs += exit_cost;
                total_trades += 1;
            }
            positions.clear();

            // Collect current rates and pick top N
            let mut candidates: Vec<(String, f64)> = funding_history
                .iter()
                .filter_map(|(sym, rates)| {
                    rates.iter().rev().find(|(rts, _)| *rts <= ts)
                        .filter(|(_, rate)| should_enter(*rate, config, 0))
                        .map(|(_, rate)| (sym.clone(), *rate))
                })
                .collect();
            candidates.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

            let to_enter = config.max_positions.min(candidates.len());
            for i in 0..to_enter {
                let (sym, _rate) = &candidates[i];
                let notional = cash / config.max_positions as f64;
                let entry_cost = notional * (config.cost_bps / 10000.0) * 2.0; // enter both legs
                cash -= entry_cost;
                total_costs += entry_cost;
                // Notional doesn't leave capital — it's deployed as spot+perp.
                // We just track it for funding accrual.
                positions.insert(sym.clone(), (ts, notional, 0.0));
            }

            last_rebalance = ts;
        } else if !positions.is_empty() {
            // Accrue funding
            let mut accrue: Vec<(String, f64)> = Vec::new();
            for (sym, (_entry_ts, notional, _accrued)) in &positions {
                let rate = funding_history
                    .get(sym)
                    .and_then(|rates| rates.iter().rev().find(|(rts, _)| *rts <= ts))
                    .map(|(_, r)| *r)
                    .unwrap_or(0.0);
                accrue.push((sym.clone(), *notional * rate));
            }
            for (sym, payment) in &accrue {
                if let Some(v) = positions.get_mut(sym) {
                    v.2 += *payment;
                    cash += *payment;
                    total_carry += *payment;
                }
            }
        }

        if cash > peak { peak = cash; }
        let dd = (peak - cash) / peak;
        if dd > max_dd { max_dd = dd; }
    }

    // Close remaining
    for v in positions.values() {
        let (_entry_ts, notional, accrued) = *v;
        let exit_cost = notional * (config.cost_bps / 10000.0) * 2.0;
        cash += accrued - exit_cost;
        total_carry += accrued;
        total_costs += exit_cost;
        total_trades += 1;
    }

    let total_return = (cash / capital - 1.0) * 100.0;
    let dd_pct = max_dd * 100.0;
    let sharpe = if duration_days > 0 && max_dd > 0.001 {
        let annual_return = total_return / (duration_days as f64 / 365.0);
        let annual_vol = max_dd * 2.0 * 100.0;
        annual_return / annual_vol
    } else {
        0.0
    };

    FundingArbResult {
        initial_capital: capital,
        final_capital: cash,
        total_return_pct: total_return,
        max_drawdown_pct: dd_pct,
        sharpe_ratio: sharpe,
        total_trades,
        total_carry_collected: total_carry,
        total_tx_costs: total_costs,
        duration_days,
        rebalances,
    }
}

fn empty_result(capital: f64) -> FundingArbResult {
    FundingArbResult {
        initial_capital: capital,
        final_capital: capital,
        total_return_pct: 0.0,
        max_drawdown_pct: 0.0,
        sharpe_ratio: 0.0,
        total_trades: 0,
        total_carry_collected: 0.0,
        total_tx_costs: 0.0,
        duration_days: 0,
        rebalances: 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn build_history(symbols: &[&str], days: i64, rates: &[f64]) -> HashMap<String, Vec<(i64, f64)>> {
        let mut history = HashMap::new();
        let start = 1700000000;
        for (i, sym) in symbols.iter().enumerate() {
            let mut records = Vec::new();
            let rate = rates[i.min(rates.len() - 1)];
            for day in 0..days {
                for period in 0..3 {
                    records.push((start + day * 86400 + period * 28800, rate));
                }
            }
            history.insert(sym.to_string(), records);
        }
        history
    }

    #[test]
    fn test_funding_arb_basic() {
        // BTC and ETH with strong positive funding, SOL negative
        // Use higher rates (0.04%) so carry dominates 40bps costs
        let history = build_history(&["BTCUSDC", "ETHUSDC", "SOLUSDC"], 60, &[0.0004, 0.0003, -0.00002]);
        let config = FundingArbConfig {
            min_rate_threshold: 0.00005,
            max_positions: 2,
            cost_bps: 10.0,
            rebalance_days: 7,
            ..Default::default()
        };
        let result = backtest_funding_arb(&history, 10000.0, &config);
        assert!(result.total_trades > 0);
        // 0.04% per 8h × 90 periods × $5k × 2 positions = $360 carry
        // vs 9 rebal × 2 pos × 40bps × $5k = $360 costs -> at 0.04% should break even
        assert!(result.total_return_pct > 0.0,
            "Return {:.2}% should be positive with 0.04% funding", result.total_return_pct);
        assert!(result.rebalances > 0);
    }

    #[test]
    fn test_negative_market_no_trades() {
        let history = build_history(&["BTCUSDC", "ETHUSDC"], 30, &[-0.0001, -0.00008]);
        let result = backtest_funding_arb(&history, 10000.0, &FundingArbConfig::default());
        assert_eq!(result.total_trades, 0);
        assert!((result.final_capital - 10000.0).abs() < 0.01);
    }

    #[test]
    fn test_single_asset_carry() {
        // 0.03% per 8h = ~0.27%/day = ~8%/month on a single asset
        let history = build_history(&["BTCUSDC"], 30, &[0.0003]);
        let config = FundingArbConfig {
            min_rate_threshold: 0.00005,
            max_positions: 1,
            cost_bps: 5.0,
            rebalance_days: 7,
            ..Default::default()
        };
        let result = backtest_funding_arb(&history, 10000.0, &config);
        assert!(result.total_return_pct > 0.0,
            "Return {:.2}% should be positive", result.total_return_pct);
        assert!(result.max_drawdown_pct < 5.0,
            "DD {:.2}% should be small", result.max_drawdown_pct);
    }
}
