/// Daily bar feed for pairs trading.
///
/// Unlike the 5m CandleFeed which monitors one symbol at a time,
/// this feed co-schedules two symbols. A new bar is only reported
/// when BOTH symbols have a new daily close.
///
/// Daily bars are fetched via REST OHLCV with "D" timeframe from Bybit.

use std::collections::HashMap;

use crate::exchange::client::ExchangeClient;
use crate::live::state_manager::StateManager;
use crate::models::candle::{ohlcv_to_candle, Candle, CandleSeries};

/// Feeds daily bars for a pair of symbols.
pub struct DailyFeed {
    exchange: ExchangeClient,
    state: StateManager,
    symbols: Vec<String>,
    lookback: usize,
    buffers: HashMap<String, Vec<Candle>>,
    series: HashMap<String, CandleSeries>,
}

impl DailyFeed {
    pub fn new(
        exchange: ExchangeClient,
        state: StateManager,
        symbols: &[String],
        lookback: usize,
    ) -> Self {
        Self {
            exchange,
            state,
            symbols: symbols.to_vec(),
            lookback,
            buffers: HashMap::new(),
            series: HashMap::new(),
        }
    }

    /// Load historical daily candles for both symbols.
    pub async fn initialize(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        for symbol in &self.symbols {
            let candles = self.load_historical(symbol).await?;
            tracing::info!(
                symbol = %symbol,
                candles = candles.len(),
                "daily_feed_initialized"
            );
            let series = Self::build_series(symbol, &candles);
            self.buffers.insert(symbol.clone(), candles);
            self.series.insert(symbol.clone(), series);
        }
        Ok(())
    }

    async fn load_historical(
        &self,
        symbol: &str,
    ) -> Result<Vec<Candle>, Box<dyn std::error::Error + Send + Sync>> {
        let since = match self
            .state
            .get_state(&format!("last_daily_{}", symbol))
            .await?
        {
            Some(ts) => {
                let dt = chrono::DateTime::parse_from_rfc3339(&ts)?;
                Some(dt.timestamp_millis())
            }
            None => None,
        };

        let rows = self
            .exchange
            .fetch_ohlcv(symbol, "D", since, Some(self.lookback as u32))
            .await?;
        let candles: Vec<Candle> = rows
            .iter()
            .map(|r| ohlcv_to_candle(r, symbol, "D"))
            .collect();
        Ok(candles)
    }

    /// Get the current CandleSeries for a symbol.
    pub fn get_series(&self, symbol: &str) -> Option<&CandleSeries> {
        self.series.get(symbol)
    }

    /// Get the closes for a symbol as a Vec<f64>.
    pub fn get_closes(&self, symbol: &str) -> Vec<f64> {
        self.series
            .get(symbol)
            .map(|s| s.closes())
            .unwrap_or_default()
    }

    /// Fetch latest candle and update buffer for a single symbol.
    async fn update_symbol(&mut self, symbol: &str) {
        let candle = match self.fetch_latest_daily(symbol).await {
            Some(c) => c,
            None => return,
        };

        let entry = self.buffers.entry(symbol.to_string());
        let buf = entry.or_default();
        if !buf.is_empty() && candle.timestamp <= buf.last().unwrap().timestamp {
            return;
        }

        buf.push(candle.clone());
        if buf.len() > self.lookback {
            *buf = buf.split_off(buf.len() - self.lookback);
        }

        let _ = self
            .state
            .set_state(
                &format!("last_daily_{}", symbol),
                &candle.timestamp.to_rfc3339(),
            )
            .await;

        let series = Self::build_series(symbol, buf);
        self.series.insert(symbol.to_string(), series);
    }

    /// Check if both symbols have a new daily bar.
    /// Returns (updated, closes_a, closes_b).
    pub async fn check_for_new_bars(&mut self) -> (bool, Vec<f64>, Vec<f64>) {
        let sym_a = self.symbols[0].clone();
        let sym_b = self.symbols[1].clone();

        self.update_symbol(&sym_a).await;
        self.update_symbol(&sym_b).await;

        let closes_a = self.get_closes(&sym_a);
        let closes_b = self.get_closes(&sym_b);
        let updated = !closes_a.is_empty() && !closes_b.is_empty();

        (updated, closes_a, closes_b)
    }

    /// Get the number of bars for the primary symbol.
    pub fn num_bars(&self) -> usize {
        self.series
            .values()
            .next()
            .map(|s| s.candles.len())
            .unwrap_or(0)
    }

    async fn fetch_latest_daily(&self, symbol: &str) -> Option<Candle> {
        match self
            .exchange
            .fetch_ohlcv(symbol, "D", None, Some(2))
            .await
        {
            Ok(rows) => {
                if rows.is_empty() {
                    return None;
                }
                let idx = if rows.len() >= 2 { rows.len() - 2 } else { 0 };
                Some(ohlcv_to_candle(&rows[idx], symbol, "D"))
            }
            Err(e) => {
                tracing::warn!(symbol = %symbol, error = %e, "fetch_daily_failed");
                None
            }
        }
    }

    fn build_series(symbol: &str, candles: &[Candle]) -> CandleSeries {
        CandleSeries {
            candles: candles.to_vec(),
            symbol: symbol.to_string(),
            exchange: "bybit".to_string(),
            timeframe: "D".to_string(),
        }
    }
}
