/// Candle (OHLCV) and CandleSeries models.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Immutable OHLCV candle data.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candle {
    pub symbol: String,
    pub exchange: String,
    pub timeframe: String,
    pub timestamp: DateTime<Utc>,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub quote_volume: f64,
    pub trades: u32,
}

impl Candle {
    /// Typical price: (H + L + C) / 3
    pub fn typical_price(&self) -> f64 {
        (self.high + self.low + self.close) / 3.0
    }

    /// High-low range.
    pub fn range(&self) -> f64 {
        self.high - self.low
    }

    /// Body size: |close - open|
    pub fn body_size(&self) -> f64 {
        (self.close - self.open).abs()
    }

    /// True if close > open.
    pub fn is_bullish(&self) -> bool {
        self.close > self.open
    }

    /// Doji detection: body < 10% of range.
    pub fn is_doji(&self) -> bool {
        self.body_size() < self.range() * 0.1
    }
}

/// Container for a series of candles with analysis helpers.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandleSeries {
    pub candles: Vec<Candle>,
    pub symbol: String,
    pub exchange: String,
    pub timeframe: String,
}

impl CandleSeries {
    /// Extract closing prices.
    pub fn closes(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.close).collect()
    }

    /// Extract high prices.
    pub fn highs(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.high).collect()
    }

    /// Extract low prices.
    pub fn lows(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.low).collect()
    }

    /// Extract volumes.
    pub fn volumes(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.volume).collect()
    }

    /// Get the n most recent candles.
    pub fn latest(&self, n: usize) -> &[Candle] {
        if n >= self.candles.len() {
            &self.candles
        } else {
            &self.candles[self.candles.len() - n..]
        }
    }

    /// Number of candles in series.
    pub fn len(&self) -> usize {
        self.candles.len()
    }

    /// Whether the series is empty.
    pub fn is_empty(&self) -> bool {
        self.candles.is_empty()
    }
}

/// Convert a raw OHLCV row [ts_ms, o, h, l, c, v] into a Candle.
pub fn ohlcv_to_candle(row: &[serde_json::Value], symbol: &str, timeframe: &str) -> Candle {
    let ts_ms = row[0].as_i64().unwrap_or(0);
    Candle {
        symbol: symbol.to_string(),
        exchange: "bybit".to_string(),
        timeframe: timeframe.to_string(),
        timestamp: DateTime::from_timestamp_millis(ts_ms).unwrap_or_default(),
        open: row[1].as_f64().unwrap_or(0.0),
        high: row[2].as_f64().unwrap_or(0.0),
        low: row[3].as_f64().unwrap_or(0.0),
        close: row[4].as_f64().unwrap_or(0.0),
        volume: row[5].as_f64().unwrap_or(0.0),
        quote_volume: 0.0,
        trades: 0,
    }
}
