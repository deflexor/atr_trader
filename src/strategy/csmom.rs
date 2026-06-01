//! Cross-sectional momentum — rank assets by lookback return, go long top N / short bottom N.
//!
//! Pure functions — no IO, no state.
//!
//! Based on the Starkiller Capital paper (Drogen, Hoffstein, Otte) and the
//! Pramesh 25-coin backtest. Key findings reflected in the design:
//!
//! - 30-day lookback / 7-day holding produces the cleanest signal (Starkiller)
//! - 20-day lookback / daily rebalance works but 138% daily turnover kills net returns
//! - 38% dispersion collapse post-2022 killed momentum — add dispersion gate
//! - Volatility-weighting beats equal-weight (lowers concentration risk)
//! - 20-50bps transaction costs must be modeled from day 1

use std::collections::HashMap;

use crate::types::candle::CandleSeries;

// ─── Config ────────────────────────────────────────────────

/// Configuration for cross-sectional momentum strategy.
#[derive(Debug, Clone)]
pub struct CSMOMConfig {
    /// Ranking period in days (e.g. 20 = 20-day lookback return)
    pub lookback_days: usize,

    /// How often to rebalance in days (e.g. 7 = weekly)
    pub rebalance_days: usize,

    /// Number of top assets to go long
    pub top_n: usize,

    /// Number of bottom assets to go short (0 = long-only)
    pub bottom_n: usize,

    /// Maximum universe size to consider
    pub universe_cap: usize,

    /// Minimum 20-day average volume (in quote currency) to include in universe
    pub min_volume_filter: f64,

    /// Use volatility parity instead of equal weight
    pub volatility_target: bool,

    /// One-way transaction cost in basis points (e.g. 20 = 0.2%)
    pub cost_bps: f64,

    /// Maximum single-asset allocation (% of portfolio, 0.0-1.0)
    pub max_allocation: f64,

    /// Minimum cross-sectional dispersion (%) to trade — skip rebalance below this
    pub min_dispersion_pct: f64,

    /// Market-neutral (sum weights = 0) or long-only
    pub market_neutral: bool,
}

impl Default for CSMOMConfig {
    fn default() -> Self {
        Self {
            lookback_days: 30,
            rebalance_days: 7,
            top_n: 3,
            bottom_n: 3,
            universe_cap: 20,
            min_volume_filter: 1_000_000.0, // $1M avg daily volume
            volatility_target: true,
            cost_bps: 20.0,
            max_allocation: 0.25,
            min_dispersion_pct: 2.0,
            market_neutral: true,
        }
    }
}

// ─── Types ─────────────────────────────────────────────────

/// A single ranked asset in the momentum ranking.
#[derive(Debug, Clone)]
pub struct RankedAsset {
    pub symbol: String,
    /// Lookback return (e.g. 0.05 = 5%)
    pub lookback_return: f64,
    /// Annualized volatility (used for vol-weighting)
    pub annualized_vol: f64,
    /// Average daily volume in quote currency
    pub avg_volume: f64,
    /// Market cap (if available, for filtering)
    pub market_cap: Option<f64>,
}

/// Complete momentum ranking result.
#[derive(Debug, Clone)]
pub struct MomentumRanking {
    pub timestamp: i64,
    pub cross_sectional_dispersion_pct: f64,
    pub eligible_count: usize,
    pub rankings: Vec<RankedAsset>,
    pub long_weights: Vec<(String, f64)>,   // (symbol, weight)
    pub short_weights: Vec<(String, f64)>,  // (symbol, weight)
    pub skipped: bool,                       // true if dispersion gate blocked
}

// ─── Ranking ───────────────────────────────────────────────

/// Rank assets by lookback return.
///
/// Takes a map of symbol → candle series, computes the lookback return for each,
/// filters by volume, and returns ranked results.
pub fn rank_momentum(
    universe: &HashMap<String, CandleSeries>,
    config: &CSMOMConfig,
) -> Vec<RankedAsset> {
    let mut ranked: Vec<RankedAsset> = Vec::new();

    for (symbol, series) in universe {
        if series.candles.len() < config.lookback_days + 1 {
            continue;
        }

        let last_idx = series.candles.len() - 1;
        let start_idx = last_idx - config.lookback_days;

        let start_close = series.candles[start_idx].close;
        let end_close = series.candles[last_idx].close;

        if start_close <= 0.0 {
            continue;
        }

        let lookback_return = (end_close - start_close) / start_close;

        // Volume filter
        let recent_candles = &series.candles[series.candles.len() - config.lookback_days..];
        let avg_volume: f64 = recent_candles.iter().map(|c| c.volume).sum::<f64>()
            / recent_candles.len() as f64;
        if avg_volume < config.min_volume_filter {
            continue;
        }

        // Compute annualized volatility from daily returns
        let daily_returns: Vec<f64> = recent_candles
            .windows(2)
            .map(|w| (w[1].close - w[0].close) / w[0].close)
            .collect();
        let annualized_vol = if daily_returns.len() >= 5 {
            let mean = daily_returns.iter().sum::<f64>() / daily_returns.len() as f64;
            let variance: f64 = daily_returns
                .iter()
                .map(|r| (r - mean).powi(2))
                .sum::<f64>()
                / (daily_returns.len() - 1) as f64;
            variance.sqrt() * (365.0_f64).sqrt() // annualize
        } else {
            0.5 // default 50% vol
        };

        ranked.push(RankedAsset {
            symbol: symbol.clone(),
            lookback_return,
            annualized_vol,
            avg_volume,
            market_cap: None,
        });
    }

    // Sort descending by lookback return
    ranked.sort_by(|a, b| b.lookback_return.partial_cmp(&a.lookback_return).unwrap_or(std::cmp::Ordering::Equal));

    // Cap universe
    if ranked.len() > config.universe_cap {
        ranked.truncate(config.universe_cap);
    }

    ranked
}

// ─── Portfolio Construction ────────────────────────────────

/// Build portfolio weights from momentum rankings.
///
/// Takes ranked assets, selects top/bottom N, assigns volatility-parity or equal weights,
/// and demeans for market neutrality.
pub fn build_portfolio_weights(
    rankings: &[RankedAsset],
    config: &CSMOMConfig,
    timestamp: i64,
) -> MomentumRanking {
    let eligible_count = rankings.len();

    // ── Compute cross-sectional dispersion ──
    let dispersion_pct = if rankings.len() >= 2 {
        let returns: Vec<f64> = rankings.iter().map(|r| r.lookback_return).collect();
        let mean = returns.iter().sum::<f64>() / returns.len() as f64;
        let variance: f64 = returns
            .iter()
            .map(|r| (r - mean).powi(2))
            .sum::<f64>()
            / (returns.len() - 1) as f64;
        variance.sqrt() * 100.0 // as percentage
    } else {
        0.0
    };

    // ── Dispersion gate ──
    if dispersion_pct < config.min_dispersion_pct || rankings.len() < config.top_n + config.bottom_n {
        return MomentumRanking {
            timestamp,
            cross_sectional_dispersion_pct: dispersion_pct,
            eligible_count,
            rankings: rankings.to_vec(),
            long_weights: Vec::new(),
            short_weights: Vec::new(),
            skipped: true,
        };
    }

    // ── Select top N and bottom N ──
    let longs: Vec<&RankedAsset> = rankings.iter().take(config.top_n).collect();
    let shorts: Vec<&RankedAsset> = rankings.iter().rev().take(config.bottom_n).collect();

    // ── Compute weights ──
    let long_weights = compute_position_weights(&longs, config, true);
    let short_weights = compute_position_weights(&shorts, config, false);

    MomentumRanking {
        timestamp,
        cross_sectional_dispersion_pct: dispersion_pct,
        eligible_count,
        rankings: rankings.to_vec(),
        long_weights,
        short_weights,
        skipped: false,
    }
}

/// Compute position weights for a set of assets.
fn compute_position_weights(
    assets: &[&RankedAsset],
    config: &CSMOMConfig,
    is_long: bool,
) -> Vec<(String, f64)> {
    if assets.is_empty() {
        return Vec::new();
    }

    let raw_weights: Vec<f64> = if config.volatility_target {
        // Inverse vol weighting: lower vol = higher weight
        assets
            .iter()
            .map(|a| {
                let vol = a.annualized_vol.max(0.1); // floor at 10% to avoid extremes
                1.0 / vol
            })
            .collect()
    } else {
        // Equal weight
        assets.iter().map(|_| 1.0).collect()
    };

    // Normalize to sum = 1.0
    let total: f64 = raw_weights.iter().sum();
    if total <= 0.0 {
        return Vec::new();
    }

    let normalized: Vec<f64> = raw_weights.iter().map(|w| w / total).collect();

    // Cap individual allocation
    let capped: Vec<f64> = normalized
        .iter()
        .map(|&w| w.min(config.max_allocation))
        .collect();

    // Re-normalize after capping
    let capped_total: f64 = capped.iter().sum();
    let final_weights: Vec<f64> = if capped_total > 0.0 {
        capped.iter().map(|&w| w / capped_total).collect()
    } else {
        capped
    };

    assets
        .iter()
        .zip(final_weights.iter())
        .map(|(a, &w)| (a.symbol.clone(), w))
        .collect()
}

/// Market-neutralize weights: subtract mean from all weights so sum = 0.
///
/// long_weights and short_weights are both positive. This function applies
/// the demean factor so the total portfolio is dollar-neutral.
pub fn market_neutralize(
    mut long_weights: Vec<(String, f64)>,
    mut short_weights: Vec<(String, f64)>,
) -> (Vec<(String, f64)>, Vec<(String, f64)>) {
    let long_sum: f64 = long_weights.iter().map(|(_, w)| w).sum();
    let short_sum: f64 = short_weights.iter().map(|(_, w)| w).sum();

    if long_sum <= 0.0 || short_sum <= 0.0 {
        return (long_weights, short_weights);
    }

    // Scale so both sides have equal total weight
    let scale = long_sum / short_sum;
    for (_, w) in short_weights.iter_mut() {
        *w *= scale;
    }

    // Now sum(long) + sum(short_scaled) = 2 * long_sum
    // Demean: subtract mean so total = 0
    // But since longs are positive and shorts are negative in implementation,
    // we want: sum(long_weights) - sum(short_weights) = 0
    // Already done by scaling

    (long_weights, short_weights)
}

/// Apply transaction costs to portfolio turnover.
///
/// Returns the estimated cost as a fraction of portfolio value.
pub fn estimate_turnover_cost(
    prev_weights: &[(String, f64)],
    new_weights: &[(String, f64)],
    cost_bps: f64,
) -> f64 {
    let cost_frac = cost_bps / 10_000.0;

    // Total absolute difference in weights = turnover
    let mut turnover: f64 = 0.0;

    for (sym, new_w) in new_weights {
        let prev_w = prev_weights
            .iter()
            .find(|(s, _)| s == sym)
            .map(|(_, w)| *w)
            .unwrap_or(0.0);
        turnover += (new_w - prev_w).abs();
    }

    // Assets that were held but are now zero
    for (sym, prev_w) in prev_weights {
        if !new_weights.iter().any(|(s, _)| s == sym) {
            turnover += prev_w;
        }
    }

    turnover * cost_frac
}

// ─── Tests ─────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::candle::Candle;

    fn make_series(symbol: &str, prices: &[f64], volume: f64) -> CandleSeries {
        let candles: Vec<Candle> = prices
            .iter()
            .enumerate()
            .map(|(i, &close)| Candle {
                symbol: symbol.into(),
                exchange: "test".into(),
                timeframe: "1d".into(),
                timestamp: 1_000_000 + i as i64 * 86400,
                open: close * 0.99,
                high: close * 1.02,
                low: close * 0.98,
                close,
                volume,
            })
            .collect();
        CandleSeries::new(candles)
    }

    fn default_config() -> CSMOMConfig {
        CSMOMConfig {
            lookback_days: 20,
            rebalance_days: 7,
            top_n: 2,
            bottom_n: 2,
            universe_cap: 10,
            min_volume_filter: 0.0, // disable for tests
            volatility_target: false,
            cost_bps: 20.0,
            max_allocation: 0.5,
            min_dispersion_pct: 0.0, // disable for tests
            market_neutral: true,
        }
    }

    #[test]
    fn test_rank_momentum_orders_correctly() {
        let mut universe = HashMap::new();
        // SOL: strong uptrend
        let sol_prices: Vec<f64> = (0..30).map(|i| 100.0 + i as f64).collect();
        universe.insert("SOLUSDC".into(), make_series("SOLUSDC", &sol_prices, 1_000_000.0));

        // BTC: flat
        let btc_prices: Vec<f64> = (0..30).map(|_| 100.0).collect();
        universe.insert("BTCUSDC".into(), make_series("BTCUSDC", &btc_prices, 1_000_000.0));

        // ETH: downtrend
        let eth_prices: Vec<f64> = (0..30).map(|i| 100.0 - i as f64).collect();
        universe.insert("ETHUSDC".into(), make_series("ETHUSDC", &eth_prices, 1_000_000.0));

        let config = default_config();
        let rankings = rank_momentum(&universe, &config);

        assert_eq!(rankings.len(), 3);
        assert_eq!(rankings[0].symbol, "SOLUSDC", "SOL should rank highest (uptrend)");
        assert_eq!(rankings[1].symbol, "BTCUSDC", "BTC should rank second (flat)");
        assert_eq!(rankings[2].symbol, "ETHUSDC", "ETH should rank lowest (downtrend)");
    }

    #[test]
    fn test_rank_momentum_insufficient_data() {
        let mut universe = HashMap::new();
        // Only 5 candles — not enough for 20-day lookback
        let prices: Vec<f64> = (0..5).map(|i| 100.0 + i as f64).collect();
        universe.insert("SOLUSDC".into(), make_series("SOLUSDC", &prices, 1_000_000.0));

        let config = default_config();
        let rankings = rank_momentum(&universe, &config);
        assert!(rankings.is_empty());
    }

    #[test]
    fn test_build_portfolio_weights_selects_top_bottom() {
        let rankings = vec![
            RankedAsset { symbol: "A".into(), lookback_return: 0.10, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "B".into(), lookback_return: 0.05, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "C".into(), lookback_return: 0.02, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "D".into(), lookback_return: -0.03, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "E".into(), lookback_return: -0.08, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
        ];

        let config = default_config(); // top_n=2, bottom_n=2
        let result = build_portfolio_weights(&rankings, &config, 1_000_000);

        assert!(!result.skipped);
        assert_eq!(result.long_weights.len(), 2);
        assert_eq!(result.short_weights.len(), 2);
        assert_eq!(result.long_weights[0].0, "A");
        assert_eq!(result.long_weights[1].0, "B");
        assert_eq!(result.short_weights[0].0, "E");
        assert_eq!(result.short_weights[1].0, "D");
    }

    #[test]
    fn test_dispersion_gate_skips_when_too_low() {
        // All assets have nearly identical returns → low dispersion
        let rankings = vec![
            RankedAsset { symbol: "A".into(), lookback_return: 0.0101, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "B".into(), lookback_return: 0.0100, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "C".into(), lookback_return: 0.0099, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "D".into(), lookback_return: 0.0098, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
        ];

        let mut config = default_config();
        config.min_dispersion_pct = 1.0; // require 1% dispersion

        let result = build_portfolio_weights(&rankings, &config, 1_000_000);
        assert!(result.skipped, "Should skip due to low dispersion");
        assert!(result.long_weights.is_empty());

        // With no gate, it should pass
        config.min_dispersion_pct = 0.0;
        let result2 = build_portfolio_weights(&rankings, &config, 1_000_000);
        assert!(!result2.skipped);
    }

    #[test]
    fn test_volume_filter() {
        let mut universe = HashMap::new();
        let sol_prices: Vec<f64> = (0..30).map(|i| 100.0 + i as f64).collect();
        universe.insert("SOLUSDC".into(), make_series("SOLUSDC", &sol_prices, 500_000.0)); // low volume

        let btc_prices: Vec<f64> = (0..30).map(|i| 100.0 + i as f64 * 0.5).collect();
        universe.insert("BTCUSDC".into(), make_series("BTCUSDC", &btc_prices, 5_000_000.0));

        let config = CSMOMConfig {
            min_volume_filter: 1_000_000.0, // $1M minimum
            ..default_config()
        };

        let rankings = rank_momentum(&universe, &config);
        assert_eq!(rankings.len(), 1, "SOL should be filtered out by low volume");
        assert_eq!(rankings[0].symbol, "BTCUSDC");
    }

    #[test]
    fn test_estimate_turnover_cost() {
        let prev = vec![("A".into(), 0.5), ("B".into(), 0.5)];
        let new = vec![("A".into(), 1.0)];

        let cost = estimate_turnover_cost(&prev, &new, 20.0);
        // A: |1.0 - 0.5| = 0.5, B: 0.5 (no longer held)
        // turnover = 1.0, cost = 1.0 * 0.002 = 0.002
        assert!((cost - 0.002).abs() < 0.0001);
    }

    #[test]
    fn test_volatility_targeting() {
        // Two assets with different volatilities
        let mut config = default_config();
        config.volatility_target = true;
        config.top_n = 2;
        config.bottom_n = 0;

        let rankings = vec![
            RankedAsset { symbol: "LOW_VOL".into(), lookback_return: 0.05, annualized_vol: 0.2, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "HIGH_VOL".into(), lookback_return: 0.10, annualized_vol: 0.8, avg_volume: 1e6, market_cap: None },
        ];

        let result = build_portfolio_weights(&rankings, &config, 1_000_000);
        assert_eq!(result.long_weights.len(), 2);

        // Low vol asset should get higher weight
        let low_vol_w = result.long_weights.iter().find(|(s, _)| s == "LOW_VOL").map(|(_, w)| *w).unwrap_or(0.0);
        let high_vol_w = result.long_weights.iter().find(|(s, _)| s == "HIGH_VOL").map(|(_, w)| *w).unwrap_or(0.0);
        assert!(low_vol_w > high_vol_w, "Low vol should get higher weight");
    }

    #[test]
    fn test_max_allocation_cap() {
        let mut config = default_config();
        config.top_n = 3;
        config.bottom_n = 0;
        config.max_allocation = 0.5;

        let rankings = vec![
            RankedAsset { symbol: "A".into(), lookback_return: 0.10, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "B".into(), lookback_return: 0.05, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
            RankedAsset { symbol: "C".into(), lookback_return: 0.02, annualized_vol: 0.3, avg_volume: 1e6, market_cap: None },
        ];

        let result = build_portfolio_weights(&rankings, &config, 1_000_000);
        for (_, w) in &result.long_weights {
            assert!(*w <= 0.51, "Weight {w} exceeds max_allocation"); // allow small float error
        }
        // With 3 assets equal weight, each would be 0.33 → no capping needed
        assert!((result.long_weights[0].1 - 0.333).abs() < 0.01);
    }
}
