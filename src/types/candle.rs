use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candle {
    pub symbol: String,
    pub exchange: String,
    pub timeframe: String,
    pub timestamp: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

impl Candle {
    pub fn typical_price(&self) -> f64 {
        (self.high + self.low + self.close) / 3.0
    }
}

#[derive(Debug, Clone)]
pub struct CandleSeries {
    pub candles: Vec<Candle>,
    pub symbol: String,
    pub timeframe: String,
}

impl CandleSeries {
    pub fn new(candles: Vec<Candle>) -> Self {
        let symbol = candles.first().map(|c| c.symbol.clone()).unwrap_or_default();
        let timeframe = candles.first().map(|c| c.timeframe.clone()).unwrap_or_default();
        Self { candles, symbol, timeframe }
    }

    pub fn closes(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.close).collect()
    }

    pub fn highs(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.high).collect()
    }

    pub fn lows(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.low).collect()
    }

    pub fn volumes(&self) -> Vec<f64> {
        self.candles.iter().map(|c| c.volume).collect()
    }

    pub fn len(&self) -> usize {
        self.candles.len()
    }

    pub fn is_empty(&self) -> bool {
        self.candles.is_empty()
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_candle() -> Candle {
        Candle {
            symbol: "SOLUSDC".into(),
            exchange: "jupiter".into(),
            timeframe: "5m".into(),
            timestamp: 1_000_000_000,
            open: 100.0,
            high: 110.0,
            low: 95.0,
            close: 105.0,
            volume: 1000.0,
        }
    }

    #[test]
    fn test_typical_price() {
        let c = sample_candle();
        let tp = c.typical_price();
        assert!((tp - 103.333).abs() < 0.01);
    }

    #[test]
    fn test_candle_serde_roundtrip() {
        let c = sample_candle();
        let json = serde_json::to_string(&c).unwrap();
        let back: Candle = serde_json::from_str(&json).unwrap();
        assert_eq!(c.symbol, back.symbol);
        assert!((c.close - back.close).abs() < f64::EPSILON);
    }

    #[test]
    fn test_candle_series() {
        let candles = vec![sample_candle(), sample_candle()];
        let series = CandleSeries::new(candles);
        assert_eq!(series.len(), 2);
        assert!(!series.is_empty());
        assert_eq!(series.closes().len(), 2);
        assert_eq!(series.symbol, "SOLUSDC");
    }
}
