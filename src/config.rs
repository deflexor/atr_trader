use std::path::PathBuf;

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub data_dir: PathBuf,
    pub log_level: String,

    #[serde(default)]
    pub market_data: MarketDataConfig,

    #[serde(default)]
    pub news: NewsConfig,

    #[serde(default)]
    pub strategy: StrategyConfig,

    #[serde(default)]
    pub signals: SignalsConfig,

    #[serde(default)]
    pub risk: RiskConfig,

    #[serde(default)]
    pub dex: DexConfig,

    #[serde(default)]
    pub live: LiveConfig,

    #[serde(default)]
    pub funding_arb: FundingArbConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MarketDataConfig {
    #[serde(default = "default_symbols")]
    pub symbols: Vec<String>,

    #[serde(default = "default_timeframe")]
    pub timeframe: String,

    #[serde(default = "default_data_source")]
    pub source: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct NewsConfig {
    #[serde(default = "default_news_sources")]
    pub sources: Vec<String>,

    #[serde(default = "default_news_poll_interval_secs")]
    pub poll_interval_secs: u64,

    #[serde(default)]
    pub cryptopanic_api_key: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct StrategyConfig {
    #[serde(default = "default_risk_per_trade")]
    pub risk_per_trade: f64,

    #[serde(default = "default_max_positions")]
    pub max_positions: usize,

    #[serde(default = "default_cooldown_candles")]
    pub cooldown_candles: u64,

    #[serde(default)]
    pub enabled_signals: Vec<String>,

    #[serde(default)]
    pub min_risk_reward: f64,

    #[serde(default = "default_stop_loss_pct")]
    pub stop_loss_pct: f64,

    #[serde(default = "default_take_profit_pct")]
    pub take_profit_pct: f64,

    #[serde(default = "default_trailing_activation_atr")]
    pub trailing_activation_atr: f64,

    #[serde(default = "default_trailing_distance_atr")]
    pub trailing_distance_atr: f64,

    #[serde(default = "default_true")]
    pub use_trailing_stop: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SignalsConfig {
    // Breakout
    #[serde(default = "default_breakout_lookback")]
    pub breakout_lookback: u64,
    #[serde(default = "default_breakout_volume_mult")]
    pub breakout_volume_mult: f64,
    #[serde(default = "default_breakout_strength")]
    pub breakout_strength: f64,

    // Mean reversion
    #[serde(default = "default_rsi_period")]
    pub rsi_period: u64,
    #[serde(default = "default_rsi_oversold")]
    pub rsi_oversold: f64,
    #[serde(default = "default_rsi_overbought")]
    pub rsi_overbought: f64,
    #[serde(default = "default_bollinger_period")]
    pub bollinger_period: u64,
    #[serde(default = "default_bollinger_std")]
    pub bollinger_std: f64,
    #[serde(default = "default_false")]
    pub bollinger_required: bool,
    #[serde(default = "default_mean_reversion_strength")]
    pub mean_reversion_strength: f64,

    // Trend
    #[serde(default = "default_ema_fast")]
    pub ema_fast: u64,
    #[serde(default = "default_ema_slow")]
    pub ema_slow: u64,
    #[serde(default = "default_macd_fast")]
    pub macd_fast: u64,
    #[serde(default = "default_macd_slow")]
    pub macd_slow: u64,
    #[serde(default = "default_macd_signal")]
    pub macd_signal: u64,
    #[serde(default = "default_min_agreement")]
    pub min_agreement: u64,
    #[serde(default = "default_trend_strength")]
    pub trend_strength: f64,

    // VWAP
    #[serde(default = "default_vwap_period")]
    pub vwap_period: u64,
    #[serde(default = "default_vwap_deviation")]
    pub vwap_deviation_threshold: f64,
    #[serde(default = "default_true")]
    pub vwap_enabled: bool,

    // Divergence
    #[serde(default = "default_divergence_lookback")]
    pub divergence_lookback: u64,
    #[serde(default = "default_divergence_rsi_threshold")]
    pub divergence_rsi_threshold: f64,
    #[serde(default = "default_true")]
    pub divergence_enabled: bool,

    // News
    #[serde(default = "default_news_lookback_hours")]
    pub news_lookback_hours: u64,

    // Filters
    #[serde(default)]
    pub min_atr_value: f64,
    #[serde(default = "default_false")]
    pub use_trend_filter: bool,
    #[serde(default = "default_trend_ema_period")]
    pub trend_ema_period: usize,

    // Multi-timeframe
    #[serde(default)]
    pub htf_enabled: bool,
    #[serde(default = "default_htf_ema_period")]
    pub htf_ema_period: usize,
    #[serde(default)]
    pub htf_allow_both: bool,
    #[serde(default = "default_htf_counter_trend_min_strength")]
    pub htf_counter_trend_min_strength: f64,
}

impl SignalsConfig {
    pub fn into_signal_config(&self) -> crate::signals::SignalConfig {
        crate::signals::SignalConfig {
            breakout_lookback: self.breakout_lookback,
            breakout_volume_mult: self.breakout_volume_mult,
            breakout_strength: self.breakout_strength,
            rsi_period: self.rsi_period,
            rsi_oversold: self.rsi_oversold,
            rsi_overbought: self.rsi_overbought,
            bollinger_period: self.bollinger_period,
            bollinger_std: self.bollinger_std,
            bollinger_required: self.bollinger_required,
            mean_reversion_strength: self.mean_reversion_strength,
            ema_fast: self.ema_fast,
            ema_slow: self.ema_slow,
            macd_fast: self.macd_fast,
            macd_slow: self.macd_slow,
            macd_signal: self.macd_signal,
            min_agreement: self.min_agreement,
            trend_strength: self.trend_strength,
            vwap_period: self.vwap_period,
            vwap_deviation_threshold: self.vwap_deviation_threshold,
            vwap_enabled: self.vwap_enabled,
            divergence_lookback: self.divergence_lookback,
            divergence_rsi_threshold: self.divergence_rsi_threshold,
            divergence_enabled: self.divergence_enabled,
            news_lookback_hours: self.news_lookback_hours,
    min_atr_value: self.min_atr_value,
    use_trend_filter: self.use_trend_filter,
    trend_ema_period: self.trend_ema_period,
    htf_enabled: self.htf_enabled,
    htf_ema_period: self.htf_ema_period,
    htf_allow_both: self.htf_allow_both,
    htf_counter_trend_min_strength: self.htf_counter_trend_min_strength,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct RiskConfig {
    #[serde(default = "default_true")]
    pub use_composite_risk: bool,

    #[serde(default = "default_total_drawdown_budget")]
    pub total_drawdown_budget: f64,

    #[serde(default = "default_per_trade_drawdown_budget")]
    pub per_trade_drawdown_budget: f64,

    #[serde(default = "default_regime_lookback")]
    pub regime_lookback: usize,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DexConfig {
    #[serde(default = "default_dex")]
    pub dex: String,

    #[serde(default = "default_rpc_url")]
    pub rpc_url: String,

    #[serde(default)]
    pub wallet_private_key: Option<String>,

    #[serde(default = "default_slippage_bps")]
    pub slippage_bps: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FundingArbConfig {
    #[serde(default = "default_exchange")]
    pub exchange: String,

    #[serde(default = "default_fa_max_positions")]
    pub max_positions: usize,

    #[serde(default = "default_fa_rebalance_days")]
    pub rebalance_days: usize,

    #[serde(default = "default_fa_cost_bps")]
    pub cost_bps: f64,

    #[serde(default = "default_fa_min_rate")]
    pub min_rate_threshold: f64,

    #[serde(default = "default_fa_max_dd")]
    pub max_drawdown_pct: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct LiveConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,

    #[serde(default = "default_candle_poll_secs")]
    pub candle_poll_secs: u64,

    #[serde(default = "default_initial_capital")]
    pub initial_capital: f64,

    #[serde(default = "default_symbols")]
    pub symbols: Vec<String>,

    /// Secondary symbol for correlation monitoring (e.g. ETH)
    pub secondary_symbol: Option<String>,

    #[serde(default = "default_true")]
    pub dry_run: bool,

    #[serde(default = "default_slippage_bps")]
    pub slippage_bps: u64,

    #[serde(default = "default_sol_price")]
    pub sol_price: f64,

    #[serde(default = "default_max_positions")]
    pub max_positions: usize,

    #[serde(default = "default_risk_per_trade")]
    pub risk_per_trade: f64,

    #[serde(default = "default_stop_loss_pct")]
    pub stop_loss_pct: f64,

    #[serde(default = "default_true")]
    pub use_trailing_stop: bool,

    #[serde(default = "default_trailing_activation_atr")]
    pub trailing_activation_atr: f64,

    #[serde(default = "default_trailing_distance_atr")]
    pub trailing_distance_atr: f64,

    #[serde(default = "default_max_drawdown_pct")]
    pub max_drawdown_pct: f64,

    #[serde(default = "default_cooldown_candles")]
    pub cooldown_candles: u64,

    #[serde(default = "default_timeframe")]
    pub timeframe: String,
}

// ── Defaults ───────────────────────────────────────────────

fn default_symbols() -> Vec<String> {
    vec!["SOLUSDC".into(), "BTCUSDC".into(), "ETHUSDC".into()]
}

fn default_timeframe() -> String {
    "5m".into()
}

fn default_data_source() -> String {
    "coingecko".into()
}

fn default_news_sources() -> Vec<String> {
    vec![
        "googlenews".into(),
        "reddit".into(),
        "coindesk".into(),
        "cointelegraph".into(),
        "decrypt".into(),
        "utoday".into(),
        "bitcoinmag".into(),
        "cryptopanic".into(),
    ]
}

fn default_news_poll_interval_secs() -> u64 {
    300
}

fn default_risk_per_trade() -> f64 {
    0.015
}

fn default_max_positions() -> usize {
    2
}

fn default_cooldown_candles() -> u64 {
    96
}

fn default_true() -> bool {
    true
}

fn default_false() -> bool {
    false
}

fn default_breakout_lookback() -> u64 { 20 }
fn default_breakout_volume_mult() -> f64 { 1.0 }
fn default_breakout_strength() -> f64 { 0.6 }
fn default_rsi_period() -> u64 { 14 }
fn default_rsi_oversold() -> f64 { 35.0 }
fn default_rsi_overbought() -> f64 { 65.0 }
fn default_bollinger_period() -> u64 { 20 }
fn default_bollinger_std() -> f64 { 2.0 }
fn default_mean_reversion_strength() -> f64 { 0.5 }
fn default_ema_fast() -> u64 { 9 }
fn default_ema_slow() -> u64 { 21 }
fn default_macd_fast() -> u64 { 12 }
fn default_macd_slow() -> u64 { 26 }
fn default_macd_signal() -> u64 { 9 }
fn default_min_agreement() -> u64 { 3 }
fn default_trend_strength() -> f64 { 0.7 }
fn default_vwap_period() -> u64 { 100 }
fn default_vwap_deviation() -> f64 { 0.02 }
fn default_divergence_lookback() -> u64 { 20 }
fn default_divergence_rsi_threshold() -> f64 { 10.0 }
fn default_news_lookback_hours() -> u64 { 24 }
fn default_trend_ema_period() -> usize { 200 }

fn default_htf_ema_period() -> usize { 50 }
fn default_htf_counter_trend_min_strength() -> f64 { 0.5 }

fn default_total_drawdown_budget() -> f64 {
    0.05
}

fn default_per_trade_drawdown_budget() -> f64 {
    0.02
}

fn default_regime_lookback() -> usize {
    100
}

fn default_dex() -> String {
    "jupiter".into()
}

fn default_rpc_url() -> String {
    "https://api.mainnet-beta.solana.com".into()
}

fn default_slippage_bps() -> u64 {
    50
}

fn default_candle_poll_secs() -> u64 {
    300
}

fn default_initial_capital() -> f64 {
    10000.0
}

fn default_sol_price() -> f64 {
    150.0
}

fn default_stop_loss_pct() -> f64 {
    0.02
}

fn default_take_profit_pct() -> f64 {
    0.04
}

fn default_trailing_activation_atr() -> f64 {
    2.5
}

fn default_trailing_distance_atr() -> f64 {
    2.5
}

fn default_max_drawdown_pct() -> f64 {
    0.05
}

fn default_exchange() -> String {
    "bybit".to_string()
}

fn default_fa_max_positions() -> usize {
    3
}

fn default_fa_rebalance_days() -> usize {
    14
}

fn default_fa_cost_bps() -> f64 {
    5.0
}

fn default_fa_min_rate() -> f64 {
    0.00005
}

fn default_fa_max_dd() -> f64 {
    0.15
}

impl Default for MarketDataConfig {
    fn default() -> Self {
        Self {
            symbols: default_symbols(),
            timeframe: default_timeframe(),
            source: default_data_source(),
        }
    }
}

impl Default for NewsConfig {
    fn default() -> Self {
        Self {
            sources: default_news_sources(),
            poll_interval_secs: default_news_poll_interval_secs(),
            cryptopanic_api_key: None,
        }
    }
}

impl Default for SignalsConfig {
    fn default() -> Self {
        Self {
            breakout_lookback: default_breakout_lookback(),
            breakout_volume_mult: default_breakout_volume_mult(),
            breakout_strength: default_breakout_strength(),
            rsi_period: default_rsi_period(),
            rsi_oversold: default_rsi_oversold(),
            rsi_overbought: default_rsi_overbought(),
            bollinger_period: default_bollinger_period(),
            bollinger_std: default_bollinger_std(),
            bollinger_required: default_false(),
            mean_reversion_strength: default_mean_reversion_strength(),
            ema_fast: default_ema_fast(),
            ema_slow: default_ema_slow(),
            macd_fast: default_macd_fast(),
            macd_slow: default_macd_slow(),
            macd_signal: default_macd_signal(),
            min_agreement: default_min_agreement(),
            trend_strength: default_trend_strength(),
            vwap_period: default_vwap_period(),
            vwap_deviation_threshold: default_vwap_deviation(),
            vwap_enabled: default_true(),
            divergence_lookback: default_divergence_lookback(),
            divergence_rsi_threshold: default_divergence_rsi_threshold(),
            divergence_enabled: default_true(),
            news_lookback_hours: default_news_lookback_hours(),
            min_atr_value: 0.0,
            use_trend_filter: false,
            trend_ema_period: default_trend_ema_period(),
            htf_enabled: false,
            htf_ema_period: default_htf_ema_period(),
            htf_allow_both: false,
            htf_counter_trend_min_strength: default_htf_counter_trend_min_strength(),
        }
    }
}

impl Default for StrategyConfig {
    fn default() -> Self {
        Self {
            risk_per_trade: default_risk_per_trade(),
            max_positions: default_max_positions(),
            cooldown_candles: default_cooldown_candles(),
            enabled_signals: vec![
                "breakout".into(),
                "mean_reversion".into(),
                "trend".into(),
                "vwap".into(),
                "divergence".into(),
                "news_sentiment".into(),
            ],
            min_risk_reward: 0.0,
            stop_loss_pct: default_stop_loss_pct(),
            take_profit_pct: default_take_profit_pct(),
            trailing_activation_atr: default_trailing_activation_atr(),
            trailing_distance_atr: default_trailing_distance_atr(),
            use_trailing_stop: true,
        }
    }
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            use_composite_risk: true,
            total_drawdown_budget: default_total_drawdown_budget(),
            per_trade_drawdown_budget: default_per_trade_drawdown_budget(),
            regime_lookback: default_regime_lookback(),
        }
    }
}

impl Default for DexConfig {
    fn default() -> Self {
        Self {
            dex: default_dex(),
            rpc_url: default_rpc_url(),
            wallet_private_key: None,
            slippage_bps: default_slippage_bps(),
        }
    }
}

impl Default for FundingArbConfig {
    fn default() -> Self {
        Self {
            exchange: default_exchange(),
            max_positions: default_fa_max_positions(),
            rebalance_days: default_fa_rebalance_days(),
            cost_bps: default_fa_cost_bps(),
            min_rate_threshold: default_fa_min_rate(),
            max_drawdown_pct: default_fa_max_dd(),
        }
    }
}

impl Default for LiveConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            candle_poll_secs: default_candle_poll_secs(),
            initial_capital: default_initial_capital(),
            symbols: default_symbols(),
            secondary_symbol: Some("ETHUSDC".into()),
            dry_run: true,
            slippage_bps: default_slippage_bps(),
            sol_price: default_sol_price(),
            max_positions: default_max_positions(),
            risk_per_trade: default_risk_per_trade(),
            stop_loss_pct: default_stop_loss_pct(),
            use_trailing_stop: true,
            trailing_activation_atr: default_trailing_activation_atr(),
            trailing_distance_atr: default_trailing_distance_atr(),
            max_drawdown_pct: default_max_drawdown_pct(),
            cooldown_candles: default_cooldown_candles(),
            timeframe: default_timeframe(),
        }
    }
}

impl Default for Config {
    fn default() -> Self {
        Self {
            data_dir: PathBuf::from("./data"),
            log_level: "info".into(),
            market_data: MarketDataConfig::default(),
            news: NewsConfig::default(),
            strategy: StrategyConfig::default(),
            signals: SignalsConfig::default(),
            risk: RiskConfig::default(),
            dex: DexConfig::default(),
            live: LiveConfig::default(),
            funding_arb: FundingArbConfig::default(),
        }
    }
}

impl Config {
    /// Load config from TOML file, merged with defaults and env overrides.
    pub fn load(path: &PathBuf) -> anyhow::Result<Self> {
        let mut config: Config = if path.exists() {
            let content = std::fs::read_to_string(path)
                .map_err(|e| anyhow::anyhow!("Failed to read config {path:?}: {e}"))?;
            toml::from_str(&content)?
        } else {
            Config::default()
        };

        // Env overrides
        if let Ok(val) = std::env::var("ATR_LOG_LEVEL") {
            config.log_level = val;
        }
        if let Ok(val) = std::env::var("ATR_DATA_DIR") {
            config.data_dir = PathBuf::from(val);
        }
        if let Ok(val) = std::env::var("ATR_CRYPTOPANIC_KEY") {
            config.news.cryptopanic_api_key = Some(val);
        }
        if let Ok(val) = std::env::var("ATR_WALLET_KEY") {
            config.dex.wallet_private_key = Some(val);
        }
        if let Ok(val) = std::env::var("ATR_RPC_URL") {
            config.dex.rpc_url = val;
        }

        Ok(config)
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = Config::default();
        assert_eq!(config.market_data.symbols.len(), 3);
        assert_eq!(config.strategy.enabled_signals.len(), 6);
        assert_eq!(config.dex.dex, "jupiter");
        assert_eq!(config.risk.total_drawdown_budget, 0.05);
    }

    #[test]
    fn test_config_from_toml() {
        let toml_str = r#"
data_dir = "./data"
log_level = "debug"

[market_data]
symbols = ["SOLUSDC"]
timeframe = "1m"

[strategy]
risk_per_trade = 0.02
        "#;

        let config: Config = toml::from_str(toml_str).unwrap();
        assert_eq!(config.market_data.symbols, vec!["SOLUSDC"]);
        assert_eq!(config.market_data.timeframe, "1m");
        assert_eq!(config.strategy.risk_per_trade, 0.02);
        // Non-overridden fields get defaults
        assert_eq!(config.news.sources.len(), 8);
    }
}
