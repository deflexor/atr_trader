/// Candle feed — REST polling for OHLCV data with rolling buffers.

use std::collections::HashMap;

use tokio::sync::broadcast;
use tracing::{debug, info, warn};

use crate::exchange::client::ExchangeClient;
use crate::live::state_manager::StateManager;
use crate::models::candle::{ohlcv_to_candle, Candle, CandleSeries};

/// Provides candle data for live trading via REST polling.
pub struct CandleFeed {
    exchange: ExchangeClient,
    state: StateManager,
    symbols: Vec<String>,
    timeframe: String,
    lookback: usize,
    market_type: String,
    buffers: HashMap<String, Vec<Candle>>,
    series: HashMap<String, CandleSeries>,
}

impl CandleFeed {
    pub fn new(
        exchange: ExchangeClient,
        state: StateManager,
        symbols: &[String],
        timeframe: &str,
        lookback: usize,
        market_type: &str,
    ) -> Self {
        Self {
            exchange,
            state,
            symbols: symbols.to_vec(),
            timeframe: timeframe.to_string(),
            lookback,
            market_type: market_type.to_string(),
            buffers: HashMap::new(),
            series: HashMap::new(),
        }
    }

    /// Load historical candles for each symbol.
    pub async fn initialize(&mut self) {
        for symbol in &self.symbols {
            match self.load_historical(symbol).await {
                Ok(candles) => {
                    info!(symbol = %symbol, candles = candles.len(), "candle_feed_initialized");
                    let series = self.build_series(symbol, &candles);
                    self.buffers.insert(symbol.clone(), candles);
                    self.series.insert(symbol.clone(), series);
                }
                Err(e) => {
                    warn!(symbol = %symbol, error = %e, "candle_feed_init_failed");
                    let empty = Vec::new();
                    let series = self.build_series(symbol, &empty);
                    self.buffers.insert(symbol.clone(), empty);
                    self.series.insert(symbol.clone(), series);
                }
            }
        }
    }

    async fn load_historical(&self, symbol: &str) -> Result<Vec<Candle>, Box<dyn std::error::Error + Send + Sync>> {
        let since = match self.state.get_state(&format!("last_candle_{}", symbol)).await? {
            Some(ts) => {
                let dt = chrono::DateTime::parse_from_rfc3339(&ts)?;
                Some(dt.timestamp_millis())
            }
            None => None,
        };

        let rows = self.exchange.fetch_ohlcv(symbol, &self.timeframe, since, Some(self.lookback as u32)).await?;
        let candles: Vec<Candle> = rows
            .iter()
            .map(|r| ohlcv_to_candle(r, symbol, &self.timeframe))
            .collect();
        Ok(candles)
    }

    /// Fetch latest completed candle and update the buffer.
    pub async fn update_candles(
        &mut self,
        symbol: &str,
    ) -> Option<CandleSeries> {
        let candle = match self.fetch_latest_candle(symbol).await {
            Some(c) => c,
            None => return None,
        };

        let buf = self.buffers.get(symbol)?;
        if !buf.is_empty() && candle.timestamp <= buf.last().unwrap().timestamp {
            return None; // No new candle
        }

        let mut updated = buf.clone();
        updated.push(candle.clone());
        if updated.len() > self.lookback {
            updated = updated.split_off(updated.len() - self.lookback);
        }

        let _ = self
            .state
            .set_state(
                &format!("last_candle_{}", symbol),
                &candle.timestamp.to_rfc3339(),
            )
            .await;

        let series = self.build_series(symbol, &updated);
        self.buffers.insert(symbol.to_string(), updated);
        self.series.insert(symbol.to_string(), series.clone());

        debug!(symbol = %symbol, ts = %candle.timestamp.to_rfc3339(), "candle_updated");
        Some(series)
    }

    /// Get current CandleSeries for a symbol.
    pub fn get_candle_series(&self, symbol: &str) -> Option<&CandleSeries> {
        self.series.get(symbol)
    }

    /// Poll every 10s until a new candle arrives, timeout, or shutdown.
    pub async fn wait_for_candle(
        &mut self,
        symbol: &str,
        shutdown: &mut broadcast::Receiver<()>,
        timeout_secs: u64,
    ) -> Option<CandleSeries> {
        let poll_interval = std::time::Duration::from_secs(10);
        let start = std::time::Instant::now();
        let timeout = std::time::Duration::from_secs(timeout_secs);

        loop {
            // Non-blocking shutdown check
            match shutdown.try_recv() {
                Ok(_) => return None,
                Err(tokio::sync::broadcast::error::TryRecvError::Empty) => {} // continue
                Err(tokio::sync::broadcast::error::TryRecvError::Closed) => return None,
                Err(tokio::sync::broadcast::error::TryRecvError::Lagged(_)) => {} // ignore lag
            }

            if let Some(series) = self.update_candles(symbol).await {
                return Some(series);
            }

            if start.elapsed() >= timeout {
                warn!(symbol = %symbol, timeout_secs, "wait_timeout");
                return None;
            }

            // Sleep with shutdown awareness
            tokio::select! {
                _ = tokio::time::sleep(poll_interval) => {},
                _ = shutdown.recv() => return None,
            }
        }
    }

    /// Fetch the most recent completed candle.
    async fn fetch_latest_candle(&self, symbol: &str) -> Option<Candle> {
        match self.exchange.fetch_ohlcv(symbol, &self.timeframe, None, Some(2)).await {
            Ok(rows) => {
                if rows.is_empty() {
                    return None;
                }
                // Use second-to-last (last fully closed candle)
                let idx = if rows.len() >= 2 { rows.len() - 2 } else { 0 };
                Some(ohlcv_to_candle(&rows[idx], symbol, &self.timeframe))
            }
            Err(e) => {
                warn!(symbol = %symbol, error = %e, "fetch_latest_failed");
                None
            }
        }
    }

    fn build_series(&self, symbol: &str, candles: &[Candle]) -> CandleSeries {
        CandleSeries {
            candles: candles.to_vec(),
            symbol: symbol.to_string(),
            exchange: "bybit".to_string(),
            timeframe: self.timeframe.clone(),
        }
    }
}
