use std::collections::{BTreeMap, HashMap};

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

    /// Aggregate candles into a higher timeframe.
    ///
    /// Supported: "1h" (60min), "4h" (240min), "1d" (1440min).
    /// Returns a new CandleSeries with aggregated candles.
    pub fn resample(&self, target_timeframe: &str) -> CandleSeries {
        let minutes: HashMap<&str, i64> = [
            ("1h", 60),
            ("4h", 240),
            ("1d", 1440),
        ].into_iter().collect();

        let tf_minutes = match minutes.get(target_timeframe) {
            Some(m) => *m,
            None => return CandleSeries {
                candles: Vec::new(),
                symbol: self.symbol.clone(),
                exchange: self.exchange.clone(),
                timeframe: target_timeframe.to_string(),
            },
        };

        let mut groups: BTreeMap<i64, Vec<&Candle>> = BTreeMap::new();
        for c in &self.candles {
            let bucket = c.timestamp.timestamp() / (tf_minutes * 60) * (tf_minutes * 60);
            groups.entry(bucket).or_default().push(c);
        }

        let aggregated: Vec<Candle> = groups
            .into_iter()
            .map(|(bucket_ts, group)| {
                let first = group[0];
                let last = *group.last().unwrap();
                Candle {
                    symbol: first.symbol.clone(),
                    exchange: first.exchange.clone(),
                    timeframe: target_timeframe.to_string(),
                    timestamp: chrono::DateTime::from_timestamp(bucket_ts, 0)
                        .unwrap_or_default(),
                    open: first.open,
                    high: group.iter().map(|c| c.high).fold(f64::NEG_INFINITY, f64::max),
                    low: group.iter().map(|c| c.low).fold(f64::INFINITY, f64::min),
                    close: last.close,
                    volume: group.iter().map(|c| c.volume).sum(),
                    quote_volume: group.iter().map(|c| c.quote_volume).sum(),
                    trades: group.iter().map(|c| c.trades).sum(),
                }
            })
            .collect();

        CandleSeries {
            candles: aggregated,
            symbol: self.symbol.clone(),
            exchange: self.exchange.clone(),
            timeframe: target_timeframe.to_string(),
        }
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
