pub mod fusion;
pub mod indicators;
pub mod news;
pub mod tech;

pub use fusion::fuse_signals;
pub use tech::{compute_breakout_signal, compute_mean_reversion_signal, compute_trend_signal, compute_vwap_signal, compute_divergence_signal};
pub use news::{compute_news_signal, NewsSignalCache};
use crate::types::SignalDirection;

/// Generate a synchronous trading signal from technical sub-signals only.
///
/// For backtesting, where async news fetching isn't available.
/// News sentiment can be added later by pre-loading signals.
pub fn generate_strategy_signal_sync(
    candles: &crate::types::CandleSeries,
    config: &SignalConfig,
    enabled_signals: &[String],
) -> crate::types::Signal {
    generate_strategy_signal_sync_with_news(candles, config, enabled_signals, None, None)
}

/// Generate a synchronous trading signal that includes news sentiment
/// from a pre-loaded cache.
///
/// This is the same as [`generate_strategy_signal_sync`] but also checks
/// the `news_cache` for recent news sentiment about the current symbol.
pub fn generate_strategy_signal_sync_with_news(
    candles: &crate::types::CandleSeries,
    config: &SignalConfig,
    enabled_signals: &[String],
    news_cache: Option<&NewsSignalCache>,
    htf_series: Option<&crate::types::CandleSeries>,
) -> crate::types::Signal {
    let price = candles.candles.last().map(|c| c.close).unwrap_or(0.0);
    let ts = candles.candles.last().map(|c| c.timestamp).unwrap_or(0);
    let symbol = &candles.symbol;

    let enabled: std::collections::HashSet<&str> =
        enabled_signals.iter().map(|s| s.as_str()).collect();

    let mut subsignals: Vec<crate::signals::tech::SubSignal> = Vec::new();

    if enabled.contains("breakout") {
        subsignals.push(compute_breakout_signal(candles, config));
    }
    if enabled.contains("mean_reversion") {
        subsignals.push(compute_mean_reversion_signal(candles, config));
    }
    if enabled.contains("trend") {
        subsignals.push(compute_trend_signal(candles, config));
    }
    if enabled.contains("vwap") {
        subsignals.push(compute_vwap_signal(candles, config));
    }
    if enabled.contains("divergence") {
        subsignals.push(compute_divergence_signal(candles, config));
    }

    // News sentiment — sync, using the pre-loaded cache
    if enabled.contains("news_sentiment") {
        if let Some(cache) = news_cache {
            let coin_key = symbol_to_cache_coin_key(symbol);
            let news_sub = cache.compute_signal(&coin_key, ts);
            if news_sub.direction.is_actionable() {
                subsignals.push(news_sub);
            }
        }
    }

    let signal = fuse_signals(&subsignals, symbol, price, ts);
    apply_signal_filters(signal, candles, config, symbol, htf_series)
}

/// Compute the higher-timeframe bias direction for a given candle timestamp.
///
/// Finds the 1h candle that contains `candle_ts`, computes an EMA on the 1h closes,
/// and returns:
/// - `Long` if 1h close > 1h EMA (uptrend → only allow longs)
/// - `Short` if 1h close < 1h EMA (downtrend → only allow shorts)
/// - `None` if insufficient data
fn compute_htf_bias(
    candle_ts: i64,
    htf_series: &crate::types::CandleSeries,
    ema_period: usize,
) -> Option<SignalDirection> {
    // Find the HTF candle at or before our candle timestamp
    let idx = htf_series.candles.iter()
        .enumerate()
        .filter(|(_, c)| c.timestamp <= candle_ts)
        .map(|(i, _)| i)
        .last()?;

    let prices: Vec<f64> = htf_series.candles.iter().map(|c| c.close).collect();

    // We need at least ema_period + 1 candles for EMA
    if idx < ema_period {
        return None;
    }

    let htf_close = htf_series.candles[idx].close;
    let htf_ema = crate::signals::indicators::ema(&prices[..=idx], ema_period)?;

    if htf_close > htf_ema {
        Some(SignalDirection::Long)
    } else if htf_close < htf_ema {
        Some(SignalDirection::Short)
    } else {
        None
    }
}

/// Apply post-fusion filters: ATR floor, trend direction gate, HTF bias.
/// Returns the signal unchanged if all pass, or a neutral signal if filtered out.
fn apply_signal_filters(
    mut signal: crate::types::Signal,
    candles: &crate::types::CandleSeries,
    config: &SignalConfig,
    symbol: &str,
    htf_series: Option<&crate::types::CandleSeries>,
) -> crate::types::Signal {
    let prices: Vec<f64> = candles.candles.iter().map(|c| c.close).collect();

    // ── ATR filter: skip if volatility is below floor ──
    if config.min_atr_value > 0.0 && signal.direction.is_actionable() {
        if let Some(atr) = compute_atr_candle(&candles.candles, 14) {
            if atr < config.min_atr_value {
                return crate::types::Signal::neutral(symbol);
            }
        }
    }

    // ── Trend filter: only trade with the EMA trend ──
    if config.use_trend_filter && signal.direction.is_actionable() {
        if let Some(trend_ema) = crate::signals::indicators::ema(&prices, config.trend_ema_period)
        {
            let price = *prices.last().unwrap_or(&0.0);
            if signal.direction.is_long() && price < trend_ema {
                return crate::types::Signal::neutral(symbol);
            }
            if signal.direction.is_short() && price > trend_ema {
                return crate::types::Signal::neutral(symbol);
            }
        }
    }

    // ── HTF (higher timeframe) bias filter ──
    if config.htf_enabled && signal.direction.is_actionable() {
        if let Some(htf) = htf_series {
            let ts = candles.candles.last().map(|c| c.timestamp).unwrap_or(0);
            let bias = compute_htf_bias(ts, htf, config.htf_ema_period);
            if config.htf_allow_both {
                // Both directions allowed — filter out weak counter-trend signals,
                // boost strong trend-aligned signals
                match (bias, signal.direction) {
                    (Some(SignalDirection::Long), SignalDirection::Long)
                    | (Some(SignalDirection::Short), SignalDirection::Short) => {
                        signal.strength *= 1.3; // Boost trend-aligned
                    }
                    (Some(bias_dir), sig_dir) if bias_dir != sig_dir => {
                        // Counter-trend: require minimum strength or reject
                        if signal.strength < config.htf_counter_trend_min_strength {
                            return crate::types::Signal::neutral(symbol);
                        }
                        signal.strength *= 0.7; // Reduce counter-trend
                    }
                    _ => {} // No bias = no adjustment
                }
            } else {
                // Restrict mode: only allow trades with the HTF trend
                match bias {
                    Some(SignalDirection::Long) => {
                        if signal.direction.is_short() {
                            return crate::types::Signal::neutral(symbol);
                        }
                    }
                    Some(SignalDirection::Short) => {
                        if signal.direction.is_long() {
                            return crate::types::Signal::neutral(symbol);
                        }
                    }
                    _ => {} // no bias = allow both
                }
            }
        }
    }

    signal
}

/// Compute ATR(period) from candles.
fn compute_atr_candle(candles: &[crate::types::Candle], period: usize) -> Option<f64> {
    if candles.len() < period + 1 || period == 0 {
        return None;
    }
    let n = candles.len();
    let mut true_ranges = Vec::with_capacity(period);
    for i in (n - period)..n {
        let c = &candles[i];
        let prev = &candles[i - 1];
        let tr = (c.high - c.low)
            .max((c.high - prev.close).abs())
            .max((c.low - prev.close).abs());
        true_ranges.push(tr);
    }
    Some(true_ranges.iter().sum::<f64>() / true_ranges.len() as f64)
}

/// Map symbol to coin key for news cache lookup.
fn symbol_to_cache_coin_key(symbol: &str) -> String {
    match symbol {
        "SOLUSDC" => "SOL".to_string(),
        "BTCUSDC" => "BTC".to_string(),
        "ETHUSDC" => "ETH".to_string(),
        s if s.ends_with("USDC") => s[..s.len() - 4].to_string(),
        s if s.ends_with("USDT") => s[..s.len() - 4].to_string(),
        _ => symbol.to_string(),
    }
}

/// Configuration for all signal types.
///
/// Mirrors the Python `EnhancedSignalConfig` — all signal parameters
/// in one place.
#[derive(Debug, Clone)]
pub struct SignalConfig {
    // Breakout
    pub breakout_lookback: u64,
    pub breakout_volume_mult: f64,
    pub breakout_strength: f64,

    // Mean reversion
    pub rsi_period: u64,
    pub rsi_oversold: f64,
    pub rsi_overbought: f64,
    pub bollinger_period: u64,
    pub bollinger_std: f64,
    pub bollinger_required: bool,
    pub mean_reversion_strength: f64,

    // Trend
    pub ema_fast: u64,
    pub ema_slow: u64,
    pub macd_fast: u64,
    pub macd_slow: u64,
    pub macd_signal: u64,
    pub min_agreement: u64,
    pub trend_strength: f64,

    // VWAP
    pub vwap_period: u64,
    pub vwap_deviation_threshold: f64,
    pub vwap_enabled: bool,

    // Divergence
    pub divergence_lookback: u64,
    pub divergence_rsi_threshold: f64,
    pub divergence_enabled: bool,

    // News sentiment
    pub news_lookback_hours: u64,

    // Filters (default 0/false = disabled)
    pub min_atr_value: f64,
    pub use_trend_filter: bool,
    pub trend_ema_period: usize,

    // Multi-timeframe (higher timeframe bias)
    pub htf_enabled: bool,
    pub htf_ema_period: usize,
    pub htf_allow_both: bool,
    /// Minimum signal strength required for counter-trend signals (htf_allow_both only)
    pub htf_counter_trend_min_strength: f64,
}

impl Default for SignalConfig {
    fn default() -> Self {
        Self {
            breakout_lookback: 20,
            breakout_volume_mult: 1.0,
            breakout_strength: 0.6,

            rsi_period: 14,
            rsi_oversold: 35.0,
            rsi_overbought: 65.0,
            bollinger_period: 20,
            bollinger_std: 2.0,
            bollinger_required: false,
            mean_reversion_strength: 0.5,

            ema_fast: 9,
            ema_slow: 21,
            macd_fast: 12,
            macd_slow: 26,
            macd_signal: 9,
            min_agreement: 3,
            trend_strength: 0.7,

            vwap_period: 100,
            vwap_deviation_threshold: 0.02,
            vwap_enabled: true,

            divergence_lookback: 20,
            divergence_rsi_threshold: 10.0,
            divergence_enabled: true,

            news_lookback_hours: 24,

            min_atr_value: 0.0,
            use_trend_filter: false,
            trend_ema_period: 200,

            // Multi-timeframe filter (higher timeframe bias)
            htf_enabled: false,
            htf_ema_period: 50,
            htf_allow_both: false,
            htf_counter_trend_min_strength: 0.5,
        }
    }
}
