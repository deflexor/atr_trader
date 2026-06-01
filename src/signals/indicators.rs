//! Technical indicator helpers — pure functions, no side effects.
//!
//! All functions take price slices and return `Option` when insufficient data.

/// Simple Moving Average
pub fn sma(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    Some(values.iter().sum::<f64>() / values.len() as f64)
}

/// Exponential Moving Average.
/// Returns the final EMA value for the full series.
/// Uses first value as seed, then computes EMA forward.
pub fn ema(prices: &[f64], period: usize) -> Option<f64> {
    if prices.is_empty() || period == 0 {
        return None;
    }
    let multiplier = 2.0 / (period as f64 + 1.0);
    let mut ema = prices[0];
    for &price in &prices[1..] {
        ema = (price - ema) * multiplier + ema;
    }
    Some(ema)
}

/// Full EMA series — returns `prices.len()` values.
///
/// Uses the first value as seed (matching Python's behavior for MACD calculation),
/// then computes EMA forward from there.
pub fn ema_series(prices: &[f64], period: usize) -> Vec<f64> {
    if prices.is_empty() || period == 0 {
        return Vec::new();
    }
    let multiplier = 2.0 / (period as f64 + 1.0);
    let mut result = Vec::with_capacity(prices.len());
    
    // Use first price as seed
    result.push(prices[0]);
    let mut ema = prices[0];
    
    for &price in &prices[1..] {
        ema = (price - ema) * multiplier + ema;
        result.push(ema);
    }
    result
}

/// Relative Strength Index.
/// Requires at least `period + 1` prices.
pub fn rsi(prices: &[f64], period: usize) -> Option<f64> {
    if prices.len() < period + 1 {
        return None;
    }
    // Compute price changes
    let deltas: Vec<f64> = prices.windows(2).map(|w| w[1] - w[0]).collect();

    // Take the last `period` deltas
    let recent = if deltas.len() > period {
        &deltas[deltas.len() - period..]
    } else {
        &deltas
    };

    if recent.is_empty() {
        return None;
    }

    let avg_gain = recent.iter().filter(|&&d| d > 0.0).copied().sum::<f64>() / period as f64;
    let avg_loss = recent.iter().filter(|&&d| d < 0.0).map(|&d| -d).sum::<f64>() / period as f64;

    if avg_loss == 0.0 && avg_gain == 0.0 {
        return Some(50.0); // No movement → neutral
    }
    if avg_loss == 0.0 {
        return Some(100.0); // All gains, no losses
    }
    let rs = avg_gain / avg_loss;
    Some(100.0 - (100.0 / (1.0 + rs)))
}

/// Bollinger Bands: (upper, middle, lower).
pub fn bollinger(prices: &[f64], period: usize, num_std: f64) -> Option<(f64, f64, f64)> {
    if prices.len() < period {
        return None;
    }
    let recent = &prices[prices.len() - period..];
    let mean = recent.iter().sum::<f64>() / period as f64;
    let variance = recent.iter().map(|&p| (p - mean).powi(2)).sum::<f64>() / period as f64;
    let std = variance.sqrt();
    let upper = mean + num_std * std;
    let lower = mean - num_std * std;
    Some((upper, mean, lower))
}

/// MACD: (macd_line, signal_line, histogram).
///
/// macd_line = ema(fast) - ema(slow)
/// signal_line = ema(macd_line, signal_period)
/// histogram = macd_line - signal_line
/// Compute ATR (Average True Range) from candle OHLC data.
pub fn atr(candles: &[crate::types::Candle], period: usize) -> Option<f64> {
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

pub fn macd(prices: &[f64], fast: usize, slow: usize, signal: usize) -> Option<(f64, f64, f64)> {
    let min_len = slow + signal;
    if prices.len() < min_len {
        return None;
    }

    // Compute full EMA series for fast and slow
    let ema_fast = ema_series(prices, fast);
    let ema_slow = ema_series(prices, slow);

    // MACD line: ema_fast - ema_slow (aligned at the end of ema_slow)
    let min_len2 = ema_fast.len().min(ema_slow.len());
    if min_len2 < signal {
        return None;
    }
    let macd_line = ema_fast[ema_fast.len() - min_len2..]
        .iter()
        .zip(&ema_slow[ema_slow.len() - min_len2..])
        .map(|(f, s)| f - s)
        .collect::<Vec<_>>();

    // Signal line: EMA of MACD line
    let sig_line = ema_series(&macd_line, signal);
    if sig_line.is_empty() {
        return None;
    }

    let macd_val = macd_line[macd_line.len() - 1];
    let sig_val = sig_line[sig_line.len() - 1];
    let hist = macd_val - sig_val;

    Some((macd_val, sig_val, hist))
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sma() {
        assert_eq!(sma(&[1.0, 2.0, 3.0]), Some(2.0));
        assert_eq!(sma(&[]), None);
    }

    #[test]
    fn test_ema() {
        let prices = vec![
            22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24, 22.29,
            22.15, 22.39, 22.38, 22.61, 23.36, 24.05, 23.75, 23.83, 23.95, 23.63,
        ];
        let ema_val = ema(&prices, 10);
        assert!(ema_val.is_some());
        // Known value: EMA-10 of this series ≈ 23.34
        let val = ema_val.unwrap();
        assert!((val - 23.34).abs() < 0.1, "EMA-10 expected ~23.34, got {val}");
    }

    #[test]
    fn test_rsi_oversold() {
        // Monotonically falling prices → RSI should be very low (near 0)
        let prices: Vec<f64> = (0..30).map(|i| 100.0 - i as f64).collect();
        let rsi_val = rsi(&prices, 14);
        assert!(rsi_val.is_some());
        let val = rsi_val.unwrap();
        assert!(val < 30.0, "RSI should be oversold, got {val}");
    }

    #[test]
    fn test_rsi_overbought() {
        // Monotonically rising prices → RSI should be very high (near 100)
        let prices: Vec<f64> = (0..30).map(|i| 100.0 + i as f64).collect();
        let rsi_val = rsi(&prices, 14);
        assert!(rsi_val.is_some());
        let val = rsi_val.unwrap();
        assert!(val > 70.0, "RSI should be overbought, got {val}");
    }

    #[test]
    fn test_rsi_neutral() {
        // Flat prices → RSI should be around 50
        let prices = vec![50.0; 30];
        let rsi_val = rsi(&prices, 14);
        assert!(rsi_val.is_some());
        let val = rsi_val.unwrap();
        assert!((val - 50.0).abs() < 0.1, "RSI should be ~50, got {val}");
    }

    #[test]
    fn test_bollinger_basic() {
        let prices = vec![10.0, 12.0, 11.0, 13.0, 10.5, 11.5, 12.5, 11.0, 10.0, 12.0];
        let bands = bollinger(&prices, 5, 2.0);
        assert!(bands.is_some());
        let (upper, middle, lower) = bands.unwrap();
        assert!(upper > middle, "Upper band should be above middle");
        assert!(lower < middle, "Lower band should be below middle");
    }

    #[test]
    fn test_macd_basic() {
        // Need at least slow + signal = 35 prices
        let mut prices = Vec::new();
        // Upward trending data
        for i in 0..50 {
            prices.push(100.0 + (i as f64 * 0.5) + (i as f64).sin() * 2.0);
        }
        let result = macd(&prices, 12, 26, 9);
        assert!(result.is_some(), "MACD should be computable with 50 prices");
        let (line, sig, hist) = result.unwrap();
        assert!(!line.is_nan(), "MACD line should not be NaN");
        assert!(!sig.is_nan(), "MACD signal should not be NaN");
        assert!(!hist.is_nan(), "MACD histogram should not be NaN");
    }

    #[test]
    fn test_insufficient_data() {
        assert!(ema(&[], 10).is_none());
        assert!(rsi(&[1.0, 2.0], 14).is_none());
        assert!(bollinger(&[1.0], 5, 2.0).is_none());
        assert!(macd(&[1.0; 20], 12, 26, 9).is_none());
    }
}
