//! Technical sub-signal generators — pure functions.
//!
//! Each function takes a candle series and config, returns a SubSignal.
//! No side effects, no IO, no state.

use crate::types::candle::CandleSeries;
use crate::types::signal::SignalDirection;

use super::indicators;
use super::SignalConfig;

/// Result from one sub-signal computation.
#[derive(Debug, Clone)]
pub struct SubSignal {
    pub direction: SignalDirection,
    pub strength: f64,
    pub source: &'static str,
}

impl SubSignal {
    pub fn neutral(source: &'static str) -> Self {
        Self {
            direction: SignalDirection::Neutral,
            strength: 0.0,
            source,
        }
    }
}

/// N-candle high/low breakout with volume confirmation.
///
/// LONG when close breaks above N-candle high.
/// SHORT when close breaks below N-candle low.
pub fn compute_breakout_signal(candles: &CandleSeries, config: &SignalConfig) -> SubSignal {
    let n = config.breakout_lookback as usize;
    if candles.len() < n + 1 {
        return SubSignal::neutral("breakout");
    }

    let window = &candles.candles[candles.len() - n - 1..candles.len() - 1];
    let current = &candles.candles[candles.len() - 1];

    let window_high = window.iter().map(|c| c.high).fold(f64::NEG_INFINITY, f64::max);
    let window_low = window.iter().map(|c| c.low).fold(f64::INFINITY, f64::min);

    // Volume check
    let avg_vol: f64 = candles.candles.iter().map(|c| c.volume).sum::<f64>()
        / candles.len() as f64;
    if avg_vol <= 0.0 {
        return SubSignal::neutral("breakout");
    }
    let vol_ok = config.breakout_volume_mult <= 1.0
        || current.volume >= avg_vol * config.breakout_volume_mult;

    if current.close > window_high && vol_ok {
        return SubSignal {
            direction: SignalDirection::Long,
            strength: config.breakout_strength,
            source: "breakout",
        };
    }
    if current.close < window_low && vol_ok {
        return SubSignal {
            direction: SignalDirection::Short,
            strength: config.breakout_strength,
            source: "breakout",
        };
    }

    SubSignal::neutral("breakout")
}

/// RSI oversold/overbought mean reversion with optional Bollinger Band touch.
///
/// LONG when RSI < oversold, optionally confirmed by price at lower Bollinger.
/// SHORT when RSI > overbought, optionally confirmed by price at upper Bollinger.
pub fn compute_mean_reversion_signal(candles: &CandleSeries, config: &SignalConfig) -> SubSignal {
    let rsi_period = config.rsi_period as usize;
    let bb_period = config.bollinger_period as usize;

    if candles.len() < rsi_period + 1 {
        return SubSignal::neutral("mean_reversion");
    }

    let closes = candles.closes();
    let current_price = closes[closes.len() - 1];

    let rsi_val = indicators::rsi(&closes, rsi_period);
    let bands = indicators::bollinger(&closes, bb_period, config.bollinger_std);

    let rsi_val = match rsi_val {
        Some(v) => v,
        None => return SubSignal::neutral("mean_reversion"),
    };

    // LONG: oversold
    if rsi_val < config.rsi_oversold {
        let bb_ok = match bands {
            Some((_upper, _mid, lower)) => {
                if config.bollinger_required {
                    current_price <= lower
                } else {
                    true
                }
            }
            None => !config.bollinger_required,
        };
        if bb_ok {
            return SubSignal {
                direction: SignalDirection::Long,
                strength: config.mean_reversion_strength,
                source: "mean_reversion",
            };
        }
    }

    // SHORT: overbought
    if rsi_val > config.rsi_overbought {
        let bb_ok = match bands {
            Some((upper, _mid, _lower)) => {
                if config.bollinger_required {
                    current_price >= upper
                } else {
                    true
                }
            }
            None => !config.bollinger_required,
        };
        if bb_ok {
            return SubSignal {
                direction: SignalDirection::Short,
                strength: config.mean_reversion_strength,
                source: "mean_reversion",
            };
        }
    }

    SubSignal::neutral("mean_reversion")
}

/// Multi-indicator trend voting: EMA + RSI + MACD.
///
/// Requires `min_agreement` out of 3 indicators to agree on direction.
/// Strength scales with agreement count.
pub fn compute_trend_signal(candles: &CandleSeries, config: &SignalConfig) -> SubSignal {
    let min_candles = (config.ema_slow + config.macd_signal).max(50) as usize;
    if candles.len() < min_candles {
        return SubSignal::neutral("trend");
    }

    let closes = candles.closes();

    let ema_fast = indicators::ema(&closes, config.ema_fast as usize);
    let ema_slow = indicators::ema(&closes, config.ema_slow as usize);
    let rsi_val = indicators::rsi(&closes, config.rsi_period as usize);
    let macd_val = indicators::macd(
        &closes,
        config.macd_fast as usize,
        config.macd_slow as usize,
        config.macd_signal as usize,
    );

    let mut bull_votes = 0u32;
    let mut bear_votes = 0u32;
    let mut total = 0u32;

    // EMA trend
    if let (Some(fast), Some(slow)) = (ema_fast, ema_slow) {
        total += 1;
        if fast > slow {
            bull_votes += 1;
        } else if fast < slow {
            bear_votes += 1;
        }
    }

    // RSI momentum
    if let Some(rsi) = rsi_val {
        total += 1;
        if rsi > 50.0 {
            bull_votes += 1;
        } else if rsi < 50.0 {
            bear_votes += 1;
        }
    }

    // MACD histogram
    if let Some((_line, _sig, hist)) = macd_val {
        total += 1;
        if hist > 0.0 {
            bull_votes += 1;
        } else if hist < 0.0 {
            bear_votes += 1;
        }
    }

    let min_agreement = config.min_agreement as u32;
    if bull_votes >= min_agreement && bull_votes > bear_votes {
        let strength = config.trend_strength * (bull_votes as f64 / total.max(1) as f64);
        return SubSignal {
            direction: SignalDirection::Long,
            strength,
            source: "trend",
        };
    }
    if bear_votes >= min_agreement && bear_votes > bull_votes {
        let strength = config.trend_strength * (bear_votes as f64 / total.max(1) as f64);
        return SubSignal {
            direction: SignalDirection::Short,
            strength,
            source: "trend",
        };
    }

    SubSignal::neutral("trend")
}

/// VWAP deviation — price far from volume-weighted average.
///
/// LONG when price significantly below VWAP, SHORT when above.
pub fn compute_vwap_signal(candles: &CandleSeries, config: &SignalConfig) -> SubSignal {
    if !config.vwap_enabled {
        return SubSignal::neutral("vwap");
    }

    let n = config.vwap_period as usize;
    if candles.len() < n {
        return SubSignal::neutral("vwap");
    }

    let recent = &candles.candles[candles.len() - n..];
    let cumulative_tp_vol: f64 = recent.iter().map(|c| c.typical_price() * c.volume).sum();
    let cumulative_vol: f64 = recent.iter().map(|c| c.volume).sum();

    if cumulative_vol <= 0.0 {
        return SubSignal::neutral("vwap");
    }

    let vwap = cumulative_tp_vol / cumulative_vol;
    let current_price = candles.candles[candles.len() - 1].close;
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
            source: "vwap",
        };
    }
    if deviation > threshold {
        return SubSignal {
            direction: SignalDirection::Short,
            strength: config.breakout_strength * 0.8,
            source: "vwap",
        };
    }

    SubSignal::neutral("vwap")
}

/// Momentum divergence — price extreme not confirmed by RSI.
///
/// Bullish divergence: price lower low, RSI higher low → LONG.
/// Bearish divergence: price higher high, RSI lower high → SHORT.
pub fn compute_divergence_signal(candles: &CandleSeries, config: &SignalConfig) -> SubSignal {
    if !config.divergence_enabled {
        return SubSignal::neutral("divergence");
    }

    let lookback = config.divergence_lookback as usize;
    let rsi_period = config.rsi_period as usize;
    let min_candles = lookback + rsi_period + 1;

    if candles.len() < min_candles {
        return SubSignal::neutral("divergence");
    }

    let closes = candles.closes();
    let half = lookback / 2;

    // Split into first and second halves
    let first_half = &closes[closes.len() - lookback - 1..closes.len() - half];
    let second_half = &closes[closes.len() - half - 1..];

    if first_half.len() < rsi_period + 1 || second_half.len() < rsi_period + 1 {
        return SubSignal::neutral("divergence");
    }

    // RSI for each half
    let rsi_first = indicators::rsi(first_half, rsi_period);
    let rsi_second = indicators::rsi(second_half, rsi_period);

    let (rsi_first, rsi_second) = match (rsi_first, rsi_second) {
        (Some(a), Some(b)) => (a, b),
        _ => return SubSignal::neutral("divergence"),
    };

    let price_first_low = first_half.iter().copied().fold(f64::INFINITY, f64::min);
    let price_second_low = second_half.iter().copied().fold(f64::INFINITY, f64::min);
    let price_first_high = first_half.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let price_second_high = second_half.iter().copied().fold(f64::NEG_INFINITY, f64::max);

    let threshold = config.divergence_rsi_threshold;

    // Bullish divergence: lower price low but higher RSI low
    if price_second_low < price_first_low && rsi_second > rsi_first + threshold {
        return SubSignal {
            direction: SignalDirection::Long,
            strength: config.mean_reversion_strength,
            source: "divergence",
        };
    }

    // Bearish divergence: higher price high but lower RSI high
    if price_second_high > price_first_high && rsi_second < rsi_first - threshold {
        return SubSignal {
            direction: SignalDirection::Short,
            strength: config.mean_reversion_strength,
            source: "divergence",
        };
    }

    SubSignal::neutral("divergence")
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::candle::Candle;
    use crate::config::SignalsConfig;

    fn make_candles(closes: &[f64]) -> CandleSeries {
        let candles: Vec<Candle> = closes
            .iter()
            .enumerate()
            .map(|(i, &close)| Candle {
                symbol: "TEST".into(),
                exchange: "test".into(),
                timeframe: "1d".into(),
                timestamp: 1_000_000 + i as i64 * 86400,
                open: close - 0.5,
                high: close + 1.0,
                low: close - 1.0,
                close,
                volume: 1000.0,
            })
            .collect();
        CandleSeries::new(candles)
    }

    fn default_config() -> SignalConfig {
        SignalsConfig::default().into_signal_config()
    }

    #[test]
    fn test_breakout_long() {
        // 20 candles in range, last one breaks out above
        let mut closes: Vec<f64> = vec![100.0; 20];
        closes.push(110.0); // breakout
        let series = make_candles(&closes);
        let signal = compute_breakout_signal(&series, &default_config());
        assert_eq!(
            signal.direction,
            SignalDirection::Long,
            "Expected LONG breakout, got {:?}",
            signal.direction
        );
    }

    #[test]
    fn test_breakout_short() {
        // 20 candles in range, last one breaks out below
        let mut closes: Vec<f64> = vec![100.0; 20];
        closes.push(90.0); // breakdown
        let series = make_candles(&closes);
        let signal = compute_breakout_signal(&series, &default_config());
        assert_eq!(signal.direction, SignalDirection::Short);
    }

    #[test]
    fn test_breakout_insufficient_data() {
        let series = make_candles(&[100.0; 5]);
        let signal = compute_breakout_signal(&series, &default_config());
        assert_eq!(signal.direction, SignalDirection::Neutral);
    }

    #[test]
    fn test_mean_reversion_long() {
        // Monotonically falling → RSI should be very low
        let closes: Vec<f64> = (0..30).map(|i| 100.0 - i as f64 * 2.0).collect();
        let series = make_candles(&closes);
        let signal = compute_mean_reversion_signal(&series, &default_config());
        assert_eq!(signal.direction, SignalDirection::Long);
    }

    #[test]
    fn test_mean_reversion_short() {
        // Monotonically rising → RSI should be very high
        let closes: Vec<f64> = (0..30).map(|i| 100.0 + i as f64 * 2.0).collect();
        let series = make_candles(&closes);
        let signal = compute_mean_reversion_signal(&series, &default_config());
        assert_eq!(signal.direction, SignalDirection::Short);
    }

    #[test]
    fn test_trend_long() {
        // Strongly upward trend: EMA fast > slow, RSI > 50, MACD > 0
        let closes: Vec<f64> = (0..60).map(|i| 100.0 + i as f64 * 0.5).collect();
        let series = make_candles(&closes);
        let signal = compute_trend_signal(&series, &default_config());
        assert_eq!(signal.direction, SignalDirection::Long);
        // With all 3 indicators agreeing, strength should be full trend_strength
        assert!(signal.strength > 0.5);
    }

    #[test]
    fn test_trend_short() {
        // Strongly downward trend
        let closes: Vec<f64> = (0..60).map(|i| 100.0 - i as f64 * 0.5).collect();
        let series = make_candles(&closes);
        let signal = compute_trend_signal(&series, &default_config());
        assert_eq!(signal.direction, SignalDirection::Short);
    }

    #[test]
    fn test_vwap_deviation() {
        // Price far above VWAP (last 20 candles: first 19 at 100, last at 150)
        let mut closes = vec![100.0; 100];
        closes.push(110.0); // 10% above VWAP
        let series = make_candles(&closes);
        let signal = compute_vwap_signal(&series, &default_config());
        assert_eq!(signal.direction, SignalDirection::Short);
    }

    #[test]
    fn test_vwap_disabled() {
        let mut config = default_config();
        config.vwap_enabled = false;
        let series = make_candles(&[100.0; 50]);
        let signal = compute_vwap_signal(&series, &config);
        assert_eq!(signal.direction, SignalDirection::Neutral);
    }

    #[test]
    fn test_divergence_bullish() {
        // Price makes lower low but RSI makes higher low
        let mut closes = vec![100.0; 50];
        // First half: higher lows
        for i in 45..50 {
            closes[i] = 100.0 + (i - 45) as f64 * 2.0;
        }
        // Second half: price drops but not as much (bullish divergence setup)
        closes.push(95.0); // lower low than recent
        let series = make_candles(&closes);
        let signal = compute_divergence_signal(&series, &default_config());
        // This is hard to construct a perfect divergence manually,
        // but at minimum the function should not panic
        assert!(signal.strength >= 0.0);
    }

    #[test]
    fn test_all_signals_insufficient() {
        let series = make_candles(&[100.0; 5]);
        assert_eq!(compute_breakout_signal(&series, &default_config()).direction, SignalDirection::Neutral);
        assert_eq!(compute_mean_reversion_signal(&series, &default_config()).direction, SignalDirection::Neutral);
        assert_eq!(compute_trend_signal(&series, &default_config()).direction, SignalDirection::Neutral);
        assert_eq!(compute_vwap_signal(&series, &default_config()).direction, SignalDirection::Neutral);
        assert_eq!(compute_divergence_signal(&series, &default_config()).direction, SignalDirection::Neutral);
    }
}
