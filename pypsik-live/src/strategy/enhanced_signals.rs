/// Enhanced signal generation — breakout, mean-reversion, trend, VWAP, and divergence.
///
/// Pure functions producing trading signals from candle data.
/// Union logic: any sub-signal fires → trade, with synergy bonus for multiple.

use crate::models::candle::CandleSeries;
use crate::models::signal::{Signal, SignalDirection, SubSignal};

/// Configuration for enhanced signal generation.
#[derive(Debug, Clone)]
pub struct EnhancedSignalConfig {
    // Breakout
    pub breakout_lookback: usize,
    pub breakout_volume_mult: f64,
    pub breakout_min_range_pct: f64,

    // Mean-reversion
    pub rsi_period: usize,
    pub rsi_oversold: f64,
    pub rsi_overbought: f64,
    pub bollinger_period: usize,
    pub bollinger_std: f64,
    pub bollinger_required: bool,

    // Trend
    pub ema_fast: usize,
    pub ema_slow: usize,
    pub macd_fast: usize,
    pub macd_slow: usize,
    pub macd_signal: usize,
    pub min_agreement: usize,

    // Strength defaults
    pub breakout_strength: f64,
    pub mean_reversion_strength: f64,
    pub trend_strength: f64,

    // VWAP
    pub vwap_period: usize,
    pub vwap_deviation_threshold: f64,
    pub vwap_enabled: bool,

    // Divergence
    pub divergence_lookback: usize,
    pub divergence_rsi_threshold: f64,
    pub divergence_enabled: bool,
}

impl Default for EnhancedSignalConfig {
    fn default() -> Self {
        Self {
            breakout_lookback: 20,
            breakout_volume_mult: 1.0,
            breakout_min_range_pct: 0.0,
            rsi_period: 14,
            rsi_oversold: 35.0,
            rsi_overbought: 65.0,
            bollinger_period: 20,
            bollinger_std: 2.0,
            bollinger_required: false,
            ema_fast: 9,
            ema_slow: 21,
            macd_fast: 12,
            macd_slow: 26,
            macd_signal: 9,
            min_agreement: 3,
            breakout_strength: 0.6,
            mean_reversion_strength: 0.5,
            trend_strength: 0.7,
            vwap_period: 100,
            vwap_deviation_threshold: 0.02,
            vwap_enabled: true,
            divergence_lookback: 20,
            divergence_rsi_threshold: 10.0,
            divergence_enabled: true,
        }
    }
}

/// Generate combined signal from all sub-strategies.
pub fn generate_enhanced_signal(
    symbol: &str,
    candles: &CandleSeries,
    config: &EnhancedSignalConfig,
) -> Signal {
    let breakout = compute_breakout_signal(candles, config);
    let mean_rev = compute_mean_reversion_signal(candles, config);
    let trend = compute_trend_signal(candles, config);
    let vwap = compute_vwap_signal(candles, config);
    let divergence = compute_divergence_signal(candles, config);

    let signals = [breakout, mean_rev, trend, vwap, divergence];
    let active: Vec<&SubSignal> = signals
        .iter()
        .filter(|s| s.direction != SignalDirection::Neutral)
        .collect();

    if active.is_empty() {
        return Signal {
            id: None,
            symbol: symbol.to_string(),
            exchange: candles.exchange.clone(),
            direction: SignalDirection::Neutral,
            strength: 0.0,
            confidence: 0.0,
            price: 0.0,
            strategy_id: "enhanced".to_string(),
            regime: None,
            risk_verdict: None,
        };
    }

    let longs: Vec<&&SubSignal> = active.iter().filter(|s| s.direction == SignalDirection::Long).collect();
    let shorts: Vec<&&SubSignal> = active.iter().filter(|s| s.direction == SignalDirection::Short).collect();

    // Conflict: both directions → no trade
    if !longs.is_empty() && !shorts.is_empty() {
        return Signal {
            id: None,
            symbol: symbol.to_string(),
            exchange: candles.exchange.clone(),
            direction: SignalDirection::Neutral,
            strength: 0.0,
            confidence: 0.0,
            price: 0.0,
            strategy_id: "enhanced".to_string(),
            regime: None,
            risk_verdict: None,
        };
    }

    let direction = if !longs.is_empty() {
        SignalDirection::Long
    } else {
        SignalDirection::Short
    };
    let agreeing = if !longs.is_empty() { &longs } else { &shorts };

    // Strength: max with synergy bonus
    let base_strength = agreeing
        .iter()
        .map(|s| s.strength)
        .fold(f64::NEG_INFINITY, f64::max);
    let synergy = 1.0 + 0.2 * (agreeing.len() - 1) as f64;
    let final_strength = (base_strength * synergy).min(1.0);

    // Confidence: fraction of signal types that agree
    let confidence = agreeing.len() as f64 / 5.0;

    let price = candles.closes().last().copied().unwrap_or(0.0);

    Signal {
        id: None,
        symbol: symbol.to_string(),
        exchange: candles.exchange.clone(),
        direction,
        strength: final_strength,
        confidence,
        price,
        strategy_id: "enhanced".to_string(),
        regime: None,
        risk_verdict: None,
    }
}

/// N-candle high/low breakout with volume confirmation.
fn compute_breakout_signal(candles: &CandleSeries, config: &EnhancedSignalConfig) -> SubSignal {
    let n = config.breakout_lookback;
    if candles.len() < n + 1 {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "breakout".to_string(),
        };
    }

    let window = &candles.candles[candles.len() - n - 1..candles.len() - 1];
    let current = &candles.candles[candles.len() - 1];
    let window_high = window.iter().map(|c| c.high).fold(f64::NEG_INFINITY, f64::max);
    let window_low = window.iter().map(|c| c.low).fold(f64::INFINITY, f64::min);

    let avg_vol = avg_volume(candles, n);
    if avg_vol <= 0.0 {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "breakout".to_string(),
        };
    }
    let vol_ok = config.breakout_volume_mult <= 1.0
        || current.volume >= avg_vol * config.breakout_volume_mult;

    let price = current.close;
    let range_pct = if price > 0.0 {
        (current.high - current.low) / price
    } else {
        0.0
    };
    let range_ok = config.breakout_min_range_pct <= 0.0 || range_pct >= config.breakout_min_range_pct;

    if current.close > window_high && vol_ok && range_ok {
        return SubSignal {
            direction: SignalDirection::Long,
            strength: config.breakout_strength,
            source: "breakout".to_string(),
        };
    }
    if current.close < window_low && vol_ok && range_ok {
        return SubSignal {
            direction: SignalDirection::Short,
            strength: config.breakout_strength,
            source: "breakout".to_string(),
        };
    }

    SubSignal {
        direction: SignalDirection::Neutral,
        strength: 0.0,
        source: "breakout".to_string(),
    }
}

/// RSI oversold/overbought bounce with optional Bollinger confirmation.
fn compute_mean_reversion_signal(candles: &CandleSeries, config: &EnhancedSignalConfig) -> SubSignal {
    let min_candles = config.bollinger_period.max(config.rsi_period) + 1;
    if candles.len() < min_candles {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "mean_reversion".to_string(),
        };
    }

    let closes = candles.closes();
    let current_price = closes[closes.len() - 1];

    let rsi = calculate_rsi(&closes, config.rsi_period);
    let bands = calculate_bollinger(&closes, config.bollinger_period, config.bollinger_std);

    match (rsi, bands) {
        (Some(rsi_val), Some((upper, _middle, lower))) => {
            // LONG: oversold
            if rsi_val < config.rsi_oversold {
                if !config.bollinger_required || current_price <= lower {
                    return SubSignal {
                        direction: SignalDirection::Long,
                        strength: config.mean_reversion_strength,
                        source: "mean_reversion".to_string(),
                    };
                }
            }
            // SHORT: overbought
            if rsi_val > config.rsi_overbought {
                if !config.bollinger_required || current_price >= upper {
                    return SubSignal {
                        direction: SignalDirection::Short,
                        strength: config.mean_reversion_strength,
                        source: "mean_reversion".to_string(),
                    };
                }
            }
            SubSignal {
                direction: SignalDirection::Neutral,
                strength: 0.0,
                source: "mean_reversion".to_string(),
            }
        }
        _ => SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "mean_reversion".to_string(),
        },
    }
}

/// Trend signal using EMA crossover + RSI + MACD voting.
fn compute_trend_signal(candles: &CandleSeries, config: &EnhancedSignalConfig) -> SubSignal {
    let min_candles = config.ema_slow + config.macd_signal;
    if candles.len() < min_candles {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "trend".to_string(),
        };
    }

    let closes = candles.closes();

    let ema_fast = calculate_ema(&closes, config.ema_fast);
    let ema_slow = calculate_ema(&closes, config.ema_slow);
    let rsi = calculate_rsi(&closes, config.rsi_period);
    let macd = calculate_macd(&closes, config.macd_fast, config.macd_slow, config.macd_signal);

    let mut bull_votes = 0usize;
    let mut bear_votes = 0usize;
    let mut total = 0usize;

    // EMA trend
    if let (Some(ef), Some(es)) = (ema_fast, ema_slow) {
        total += 1;
        if ef > es {
            bull_votes += 1;
        } else if ef < es {
            bear_votes += 1;
        }
    }

    // RSI momentum
    if let Some(rsi_val) = rsi {
        total += 1;
        if rsi_val > 50.0 {
            bull_votes += 1;
        } else if rsi_val < 50.0 {
            bear_votes += 1;
        }
    }

    // MACD histogram
    if let Some((_line, _sig, histogram)) = macd {
        total += 1;
        if histogram > 0.0 {
            bull_votes += 1;
        } else if histogram < 0.0 {
            bear_votes += 1;
        }
    }

    if bull_votes >= config.min_agreement && bull_votes > bear_votes {
        let strength = config.trend_strength * (bull_votes as f64 / total.max(1) as f64);
        return SubSignal {
            direction: SignalDirection::Long,
            strength,
            source: "trend".to_string(),
        };
    }
    if bear_votes >= config.min_agreement && bear_votes > bull_votes {
        let strength = config.trend_strength * (bear_votes as f64 / total.max(1) as f64);
        return SubSignal {
            direction: SignalDirection::Short,
            strength,
            source: "trend".to_string(),
        };
    }

    SubSignal {
        direction: SignalDirection::Neutral,
        strength: 0.0,
        source: "trend".to_string(),
    }
}

/// VWAP deviation signal — mean-reversion to volume-weighted price.
fn compute_vwap_signal(candles: &CandleSeries, config: &EnhancedSignalConfig) -> SubSignal {
    if !config.vwap_enabled {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "vwap".to_string(),
        };
    }

    let n = config.vwap_period;
    if candles.len() < n {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "vwap".to_string(),
        };
    }

    let recent = &candles.candles[candles.len() - n..];
    let cumulative_tp_vol: f64 = recent.iter().map(|c| c.typical_price() * c.volume).sum();
    let cumulative_vol: f64 = recent.iter().map(|c| c.volume).sum();

    if cumulative_vol <= 0.0 {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "vwap".to_string(),
        };
    }

    let vwap = cumulative_tp_vol / cumulative_vol;
    let current_price = candles.candles.last().unwrap().close;
    let deviation = if vwap > 0.0 {
        (current_price - vwap) / vwap
    } else {
        0.0
    };

    let threshold = config.vwap_deviation_threshold;
    if deviation < -threshold {
        return SubSignal {
            direction: SignalDirection::Long,
            strength: config.breakout_strength * 0.8,
            source: "vwap".to_string(),
        };
    }
    if deviation > threshold {
        return SubSignal {
            direction: SignalDirection::Short,
            strength: config.breakout_strength * 0.8,
            source: "vwap".to_string(),
        };
    }

    SubSignal {
        direction: SignalDirection::Neutral,
        strength: 0.0,
        source: "vwap".to_string(),
    }
}

/// Momentum divergence — price makes new extreme but RSI doesn't.
fn compute_divergence_signal(candles: &CandleSeries, config: &EnhancedSignalConfig) -> SubSignal {
    if !config.divergence_enabled {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "divergence".to_string(),
        };
    }

    let lookback = config.divergence_lookback;
    let rsi_period = config.rsi_period;
    let min_candles = lookback + rsi_period + 1;

    if candles.len() < min_candles {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "divergence".to_string(),
        };
    }

    let closes = candles.closes();
    let half = lookback / 2;

    let first_half = &closes[closes.len() - lookback - 1..closes.len() - half];
    let second_half = &closes[closes.len() - half - 1..];

    if first_half.len() < rsi_period + 1 || second_half.len() < rsi_period + 1 {
        return SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "divergence".to_string(),
        };
    }

    // Compute RSI for each half with full context
    let full_first = &closes[..closes.len() - half];
    let full_second = &closes[..];

    let rsi_first = calculate_rsi(full_first, rsi_period);
    let rsi_second = calculate_rsi(full_second, rsi_period);

    match (rsi_first, rsi_second) {
        (Some(rf), Some(rs)) => {
            let price_first_low = first_half.iter().cloned().fold(f64::INFINITY, f64::min);
            let price_second_low = second_half.iter().cloned().fold(f64::INFINITY, f64::min);
            let price_first_high = first_half.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let price_second_high = second_half.iter().cloned().fold(f64::NEG_INFINITY, f64::max);

            let threshold = config.divergence_rsi_threshold;

            // Bullish divergence
            if price_second_low < price_first_low && rs > rf + threshold {
                return SubSignal {
                    direction: SignalDirection::Long,
                    strength: config.mean_reversion_strength,
                    source: "divergence".to_string(),
                };
            }

            // Bearish divergence
            if price_second_high > price_first_high && rs < rf - threshold {
                return SubSignal {
                    direction: SignalDirection::Short,
                    strength: config.mean_reversion_strength,
                    source: "divergence".to_string(),
                };
            }

            SubSignal {
                direction: SignalDirection::Neutral,
                strength: 0.0,
                source: "divergence".to_string(),
            }
        }
        _ => SubSignal {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source: "divergence".to_string(),
        },
    }
}

// ── Technical indicators (pure functions) ──────────────────

fn calculate_ema(prices: &[f64], period: usize) -> Option<f64> {
    if prices.len() < period {
        return None;
    }
    let multiplier = 2.0 / (period as f64 + 1.0);
    let mut ema: f64 = prices[..period].iter().sum::<f64>() / period as f64;
    for price in &prices[period..] {
        ema = (price - ema) * multiplier + ema;
    }
    Some(ema)
}

fn calculate_rsi(prices: &[f64], period: usize) -> Option<f64> {
    if prices.len() < period + 1 {
        return None;
    }

    let deltas: Vec<f64> = prices[1..]
        .iter()
        .zip(prices.iter())
        .map(|(curr, prev)| curr - prev)
        .collect();

    let recent = &deltas[deltas.len() - period..];
    let gains: Vec<f64> = recent.iter().map(|d| d.max(0.0)).collect();
    let losses: Vec<f64> = recent.iter().map(|d| (-d).max(0.0)).collect();

    let avg_gain: f64 = gains.iter().sum::<f64>() / gains.len() as f64;
    let avg_loss: f64 = losses.iter().sum::<f64>() / losses.len() as f64;

    if avg_loss == 0.0 {
        return Some(100.0);
    }

    let rs = avg_gain / avg_loss;
    Some(100.0 - (100.0 / (1.0 + rs)))
}

fn calculate_bollinger(prices: &[f64], period: usize, num_std: f64) -> Option<(f64, f64, f64)> {
    if prices.len() < period {
        return None;
    }
    let recent = &prices[prices.len() - period..];
    let middle: f64 = recent.iter().sum::<f64>() / recent.len() as f64;

    let variance: f64 = recent
        .iter()
        .map(|p| (p - middle).powi(2))
        .sum::<f64>()
        / recent.len() as f64;
    let std = variance.sqrt();

    Some((middle + num_std * std, middle, middle - num_std * std))
}

fn calculate_macd(
    prices: &[f64],
    fast: usize,
    slow: usize,
    signal: usize,
) -> Option<(f64, f64, f64)> {
    if prices.len() < slow + signal {
        return None;
    }

    let ema_f = ema_series(prices, fast);
    let ema_s = ema_series(prices, slow);

    let macd_line: Vec<f64> = ema_s
        .iter()
        .zip(ema_f.iter())
        .map(|(s, f)| f - s)
        .collect();

    if macd_line.len() < signal {
        return None;
    }

    let sig_line = ema_series(&macd_line, signal);
    if sig_line.is_empty() {
        return None;
    }

    let histogram = *macd_line.last()? - *sig_line.last()?;
    Some((*macd_line.last()?, *sig_line.last()?, histogram))
}

fn ema_series(data: &[f64], period: usize) -> Vec<f64> {
    if data.is_empty() {
        return Vec::new();
    }
    let mult = 2.0 / (period as f64 + 1.0);
    let mut result = vec![data[0]];
    for p in &data[1..] {
        result.push((p - result.last().unwrap()) * mult + result.last().unwrap());
    }
    result
}

fn avg_volume(candles: &CandleSeries, lookback: usize) -> f64 {
    let n = lookback.min(candles.len());
    if n == 0 {
        return 0.0;
    }
    let recent = &candles.candles[candles.len() - n..];
    recent.iter().map(|c| c.volume).sum::<f64>() / n as f64
}
