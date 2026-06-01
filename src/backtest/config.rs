//! Backtest configuration.
//!
//! DEX fee model from day 1: uses price impact + gas instead of
//! order-book commissions.

/// Configuration for backtesting.
#[derive(Debug, Clone)]
pub struct BacktestConfig {
    pub initial_capital: f64,
    pub slippage_bps: u64,
    pub gas_cost_sol: f64,
    pub sol_price: f64,

    // Position management
    pub max_positions: usize,
    pub pyramid_entries: usize,
    pub entry_spacing_pct: f64,
    pub cooldown_candles: i64,

    // Risk per trade (% of capital)
    pub risk_per_trade: f64,

    // Stop loss / take profit
    pub stop_loss_pct: f64,
    pub take_profit_pct: f64,

    // Trailing stop
    pub use_trailing_stop: bool,
    pub trailing_activation_atr: f64,
    pub trailing_distance_atr: f64,
    pub atr_period: usize,

    // Drawdown halt
    pub max_drawdown_pct: f64,

    // Risk integration
    pub use_composite_risk: bool,
    pub regime_lookback: usize,
    pub vwlp_window_candles: usize,
    pub vwlp_min_samples: usize,

    // Correlation
    pub use_correlation: bool,
    pub correlation_lookback: usize,

    // Risk:Reward filter — skip trades below this ratio
    pub min_risk_reward: f64,
}

impl Default for BacktestConfig {
    fn default() -> Self {
        Self {
            initial_capital: 10000.0,
            slippage_bps: 50,
            gas_cost_sol: 0.000_005,
            sol_price: 150.0,

            max_positions: 3,
            pyramid_entries: 2,
            entry_spacing_pct: 0.005,
            cooldown_candles: 96,

            risk_per_trade: 0.015,

            stop_loss_pct: 0.02,
            take_profit_pct: 0.04,

            use_trailing_stop: true,
            trailing_activation_atr: 2.5,
            trailing_distance_atr: 2.5,
            atr_period: 14,

            max_drawdown_pct: 0.05,

            use_composite_risk: true,
            regime_lookback: 100,
            vwlp_window_candles: 5,
            vwlp_min_samples: 3,

            use_correlation: false,
            correlation_lookback: 20,

            min_risk_reward: 0.0, // 0 = disabled
        }
    }
}
