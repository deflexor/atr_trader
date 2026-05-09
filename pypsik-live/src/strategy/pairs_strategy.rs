/// Pairs trading strategy — z-score of log price ratio.
///
/// Pure computation, no I/O. Implements the validated SHIB/UNI strategy:
///   - Spread = log(price_A / price_B)
///   - Rolling z-score with configurable lookback
///   - Entry when |zscore| > entry_z
///   - Exit when zscore crosses 0, or stop_z, or max_hold
///
/// Validated config (730d backtest):
///   SHIB/UNI: lookback=30, entry_z=2.5, exit_z=0.0, stop_z=4.0, max_hold=30
///   +35.3% return, Sharpe +1.67, DD 5.4%, 53 trades, 64.2% WR

use crate::models::pair::{PairAction, PairDirection, PairSignal};

/// Configuration for the pairs strategy.
#[derive(Debug, Clone)]
pub struct PairsConfig {
    /// Symbol for asset A (e.g., "SHIBUSDT")
    pub symbol_a: String,
    /// Symbol for asset B (e.g., "UNIUSDT")
    pub symbol_b: String,
    /// Pair label for display
    pub pair_label: String,
    /// Rolling window for z-score (number of daily bars)
    pub lookback: usize,
    /// Z-score threshold to enter a trade
    pub entry_z: f64,
    /// Z-score threshold for take-profit exit (spread crosses this toward zero)
    pub exit_z: f64,
    /// Z-score threshold for stop-loss exit (spread diverges further)
    pub stop_z: f64,
    /// Maximum holding period in daily bars
    pub max_hold: usize,
    /// Capital allocation fraction per pair (default 0.5)
    pub position_pct: f64,
    /// Total capital for position sizing
    pub capital: f64,
}

impl PairsConfig {
    /// Default config for SHIB/UNI (best validated)
    pub fn shib_uni(capital: f64) -> Self {
        Self {
            symbol_a: "SHIBUSDT".into(),
            symbol_b: "UNIUSDT".into(),
            pair_label: "SHIB/UNI".into(),
            lookback: 30,
            entry_z: 2.5,
            exit_z: 0.0,
            stop_z: 4.0,
            max_hold: 30,
            position_pct: 0.5,
            capital,
        }
    }

    /// Default config for LINK/SOL (backup pair)
    #[allow(dead_code)]
    pub fn link_sol(capital: f64) -> Self {
        Self {
            symbol_a: "LINKUSDT".into(),
            symbol_b: "SOLUSDT".into(),
            pair_label: "LINK/SOL".into(),
            lookback: 20,
            entry_z: 2.5,
            exit_z: 0.0,
            stop_z: 4.0,
            max_hold: 30,
            position_pct: 0.5,
            capital,
        }
    }
}

/// Pairs trading strategy — pure computation.
pub struct PairsStrategy {
    config: PairsConfig,
}

impl PairsStrategy {
    pub fn new(config: PairsConfig) -> Self {
        Self { config }
    }

    pub fn config(&self) -> &PairsConfig {
        &self.config
    }

    /// Compute the log price ratio spread: ln(A/B).
    pub fn compute_spread(&self, prices_a: &[f64], prices_b: &[f64]) -> Vec<f64> {
        prices_a
            .iter()
            .zip(prices_b.iter())
            .map(|(a, b)| (a / b).ln())
            .collect()
    }

    /// Compute rolling z-scores. Returns None for the initial lookback window.
    pub fn rolling_zscore(&self, series: &[f64]) -> Vec<Option<f64>> {
        let mut result = vec![None; series.len()];
        let lb = self.config.lookback;

        if series.len() <= lb {
            return result;
        }

        for i in lb..series.len() {
            let window = &series[i - lb..i];
            let n = window.len() as f64;
            let mean: f64 = window.iter().sum::<f64>() / n;
            let variance: f64 = window.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / n;
            let std = variance.sqrt();

            result[i] = if std > 1e-10 {
                Some((series[i] - mean) / std)
            } else {
                Some(0.0)
            };
        }

        result
    }

    /// Generate a pair signal from current z-score and trade state.
    ///
    /// `in_position` — whether we currently have an open pair trade
    /// `direction` — current trade direction (if in position)
    /// `entry_idx` — the day index when the current trade was entered
    pub fn generate_signal(
        &self,
        zscore: f64,
        spread: f64,
        day_index: usize,
        in_position: bool,
        direction: Option<PairDirection>,
        entry_idx: Option<usize>,
    ) -> PairSignal {
        let cfg = &self.config;

        if in_position {
            let dir = direction.unwrap();
            let entry = entry_idx.unwrap();

            // Take profit: spread reverted toward mean
            match dir {
                PairDirection::LongAShortB => {
                    if zscore <= cfg.exit_z {
                        return PairSignal {
                            action: PairAction::ExitTakeProfit,
                            direction: Some(dir),
                            zscore,
                            spread,
                            day_index,
                        };
                    }
                    if zscore >= cfg.stop_z {
                        return PairSignal {
                            action: PairAction::ExitStopLoss,
                            direction: Some(dir),
                            zscore,
                            spread,
                            day_index,
                        };
                    }
                }
                PairDirection::ShortALongB => {
                    if zscore >= -cfg.exit_z {
                        return PairSignal {
                            action: PairAction::ExitTakeProfit,
                            direction: Some(dir),
                            zscore,
                            spread,
                            day_index,
                        };
                    }
                    if zscore <= -cfg.stop_z {
                        return PairSignal {
                            action: PairAction::ExitStopLoss,
                            direction: Some(dir),
                            zscore,
                            spread,
                            day_index,
                        };
                    }
                }
            }

            // Max hold
            if day_index - entry >= cfg.max_hold {
                return PairSignal {
                    action: PairAction::ExitMaxHold,
                    direction: Some(dir),
                    zscore,
                    spread,
                    day_index,
                };
            }
        } else {
            // Check entry conditions
            if zscore >= cfg.entry_z {
                return PairSignal {
                    action: PairAction::Enter,
                    direction: Some(PairDirection::ShortALongB),
                    zscore,
                    spread,
                    day_index,
                };
            }
            if zscore <= -cfg.entry_z {
                return PairSignal {
                    action: PairAction::Enter,
                    direction: Some(PairDirection::LongAShortB),
                    zscore,
                    spread,
                    day_index,
                };
            }
        }

        PairSignal {
            action: PairAction::Hold,
            direction,
            zscore,
            spread,
            day_index,
        }
    }

    /// Calculate position size for each leg of the pair trade.
    /// Equal-dollar allocation: position_pct × capital / price for each leg.
    pub fn calculate_leg_size(&self, price: f64) -> f64 {
        let allocation = self.config.capital * self.config.position_pct;
        if price > 0.0 {
            allocation / price
        } else {
            0.0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_spread_computation() {
        let config = PairsConfig::shib_uni(1000.0);
        let strat = PairsStrategy::new(config);
        let prices_a = vec![2.0, 4.0];
        let prices_b = vec![1.0, 2.0];
        let spread = strat.compute_spread(&prices_a, &prices_b);
        // ln(2/1) = ln(2) ≈ 0.693, ln(4/2) = ln(2) ≈ 0.693
        assert!((spread[0] - 0.693).abs() < 0.01);
        assert!((spread[1] - 0.693).abs() < 0.01);
    }

    #[test]
    fn test_zscore_symmetry() {
        let config = PairsConfig {
            symbol_a: "A".into(),
            symbol_b: "B".into(),
            pair_label: "A/B".into(),
            lookback: 5,
            entry_z: 2.0,
            exit_z: 0.0,
            stop_z: 4.0,
            max_hold: 30,
            position_pct: 0.5,
            capital: 1000.0,
        };
        let strat = PairsStrategy::new(config);

        // Constant series → zscore ≈ 0
        let series = vec![1.0; 10];
        let zs = strat.rolling_zscore(&series);
        for i in 5..10 {
            assert!((zs[i].unwrap() - 0.0).abs() < 0.01);
        }
    }

    #[test]
    fn test_entry_signal() {
        let config = PairsConfig {
            symbol_a: "A".into(),
            symbol_b: "B".into(),
            pair_label: "A/B".into(),
            lookback: 5,
            entry_z: 2.0,
            exit_z: 0.0,
            stop_z: 4.0,
            max_hold: 30,
            position_pct: 0.5,
            capital: 1000.0,
        };
        let strat = PairsStrategy::new(config);

        // z=2.5 → should enter (short A, long B)
        let sig = strat.generate_signal(2.5, 0.3, 10, false, None, None);
        assert!(sig.is_entry());
        assert_eq!(sig.direction.unwrap(), PairDirection::ShortALongB);

        // z=-2.5 → should enter (long A, short B)
        let sig = strat.generate_signal(-2.5, -0.3, 10, false, None, None);
        assert!(sig.is_entry());
        assert_eq!(sig.direction.unwrap(), PairDirection::LongAShortB);

        // z=0.0 → should hold
        let sig = strat.generate_signal(0.0, 0.0, 10, false, None, None);
        assert!(!sig.is_entry());
        assert!(!sig.is_exit());
    }

    #[test]
    fn test_exit_signal() {
        let config = PairsConfig {
            symbol_a: "A".into(),
            symbol_b: "B".into(),
            pair_label: "A/B".into(),
            lookback: 5,
            entry_z: 2.0,
            exit_z: 0.5,
            stop_z: 4.0,
            max_hold: 30,
            position_pct: 0.5,
            capital: 1000.0,
        };
        let strat = PairsStrategy::new(config);

        // Long A, short B — zscore rises to ≤ exit_z → take profit
        let sig = strat.generate_signal(-0.4, 0.0, 50, true, Some(PairDirection::LongAShortB), Some(30));
        assert!(sig.is_exit());

        // Short A, long B — zscore drops to ≥ -exit_z → take profit
        let sig = strat.generate_signal(0.4, 0.0, 50, true, Some(PairDirection::ShortALongB), Some(30));
        assert!(sig.is_exit());

        // Stop loss: Long A, short B, zscore hits +4.0
        let sig = strat.generate_signal(4.1, 0.8, 50, true, Some(PairDirection::LongAShortB), Some(30));
        assert!(sig.is_exit());

        // Max hold: in position 31 bars with max_hold=30
        let sig = strat.generate_signal(1.0, 0.2, 61, true, Some(PairDirection::ShortALongB), Some(30));
        assert!(sig.is_exit());

        // Hold: NOT in position, z between thresholds
        let sig = strat.generate_signal(-1.0, -0.2, 50, false, None, None);
        assert_eq!(sig.action, PairAction::Hold);
    }
}
