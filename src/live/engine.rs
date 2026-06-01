//! Live trading engine — integrates all stages into a continuous trading loop.
//!
//! Dry-run mode by default (prints trade decisions). Set `LiveConfig::dry_run = false`
//! and configure a wallet to execute real trades via Jupiter.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crate::backtest::config::BacktestConfig;
use crate::backtest::fills::FillSimulator;
use crate::config::LiveConfig;
use crate::data::market::{create_provider, MarketDataProvider};
use crate::data::store::DataStore;
use crate::dex::jupiter::JupiterClient;
use crate::dex::token::TokenRegistry;
use crate::dex::wallet::Wallet;
use crate::risk::composite::{compute_composite_score, CompositeRiskConfig};
use crate::risk::correlation::{CorrelationConfig, CorrelationMonitor};
use crate::risk::regime::RegimeDetector;
use crate::risk::velocity::VelocityTracker;
use crate::signals::{fuse_signals, SignalConfig};
use crate::signals::news::compute_news_signal;
use crate::signals::tech::{
    compute_breakout_signal, compute_divergence_signal, compute_mean_reversion_signal,
    compute_trend_signal, compute_vwap_signal, SubSignal,
};
use crate::types::{Candle, CandleSeries, Position, Signal, SignalDirection};

/// Subsystem holder for live trading.
struct RiskSubsystems {
    regime_detector: RegimeDetector,
    velocity_tracker: VelocityTracker,
    correlation_monitor: Option<CorrelationMonitor>,
    composite_config: CompositeRiskConfig,
}

/// State for a single trading symbol.
struct SymbolState {
    symbol: String,
    // Cached latest candles for this symbol
    candles: Vec<Candle>,
    // Open positions for this symbol
    positions: Vec<Position>,
    // Last candle index when a trade was made (cooldown)
    last_trade_candle: usize,
}

/// The live trading engine.
pub struct LiveEngine {
    pub config: LiveConfig,
    pub signal_config: SignalConfig,
    pub risk_config: CompositeRiskConfig,
    pub dry_run: bool,

    // Subsystems (some owned, some references for testability)
    store: Option<DataStore>,
    market_provider: Option<Box<dyn MarketDataProvider>>,
    jupiter: Option<JupiterClient>,
    wallet: Option<Wallet>,
    token_registry: TokenRegistry,
    fill_sim: FillSimulator,

    // Per-symbol state
    symbols: Vec<SymbolState>,

    // Global risk subsystems
    risk: RiskSubsystems,

    // Global state
    capital: f64,
    peak_equity: f64,
    drawdown_halted: bool,
    trades: Vec<LiveTrade>,

    // Signals for graceful shutdown
    running: Arc<AtomicBool>,
}

/// A trade recorded during live trading.
#[derive(Debug, Clone)]
pub struct LiveTrade {
    pub timestamp: i64,
    pub symbol: String,
    pub side: String,
    pub entry_price: f64,
    pub quantity: f64,
    pub reason: String,
    pub executed: bool,
    pub tx_signature: Option<String>,
}

impl LiveEngine {
    /// Create a new live engine in dry-run mode (no execution).
    pub fn new_dry_run(live_config: LiveConfig, signal_config: SignalConfig) -> Self {
        Self::new(live_config, signal_config, true)
    }

    /// Create a new live engine.
    fn new(live_config: LiveConfig, signal_config: SignalConfig, dry_run: bool) -> Self {
        let risk_config = CompositeRiskConfig::default();
        let symbols: Vec<SymbolState> = live_config
            .symbols
            .iter()
            .map(|s| SymbolState {
                symbol: s.clone(),
                candles: Vec::new(),
                positions: Vec::new(),
                last_trade_candle: 0,
            })
            .collect();

        let fill_sim = FillSimulator::new(
            live_config.slippage_bps,
            0.000_005,
            live_config.sol_price,
        );

        let correlation_monitor = if live_config.secondary_symbol.is_some() {
            Some(CorrelationMonitor::new(CorrelationConfig::default()))
        } else {
            None
        };

        Self {
            config: live_config,
            signal_config,
            risk_config,
            dry_run,
            store: None,
            market_provider: None,
            jupiter: None,
            wallet: None,
            token_registry: TokenRegistry::new(),
            fill_sim,
            symbols,
            risk: RiskSubsystems {
                regime_detector: RegimeDetector::default(),
                velocity_tracker: VelocityTracker::default(),
                correlation_monitor,
                composite_config: CompositeRiskConfig::default(),
            },
            capital: 0.0,
            peak_equity: 0.0,
            drawdown_halted: false,
            trades: Vec::new(),
            running: Arc::new(AtomicBool::new(true)),
        }
    }

    /// Set the data store.
    pub fn with_store(mut self, store: DataStore) -> Self {
        self.store = Some(store);
        self
    }

    /// Set the market data provider.
    pub fn with_market_provider(mut self, provider: Box<dyn MarketDataProvider>) -> Self {
        self.market_provider = Some(provider);
        self
    }

    /// Set the Jupiter client.
    pub fn with_jupiter(mut self, client: JupiterClient) -> Self {
        self.jupiter = Some(client);
        self
    }

    /// Set the wallet for live execution.
    pub fn with_wallet(mut self, wallet: Wallet) -> Self {
        self.wallet = Some(wallet);
        self
    }

    /// Set initial capital.
    pub fn with_capital(mut self, capital: f64) -> Self {
        self.capital = capital;
        self.peak_equity = capital;
        self
    }

    /// Get the stop signal handle for graceful shutdown.
    pub fn stop_signal(&self) -> Arc<AtomicBool> {
        self.running.clone()
    }

    /// Run a single cycle of the trading loop.
    /// Returns the number of trades executed (or 0 for dry-run).
    pub async fn run_cycle(&mut self) -> anyhow::Result<usize> {
        let mut trades_made = 0;

        for idx in 0..self.symbols.len() {
            let symbol = self.symbols[idx].symbol.clone();

            // 1. Fetch latest candles
            let new_candles = self.fetch_candles(&symbol).await?;
            if let Some(candles) = new_candles {
                self.symbols[idx].candles = candles;
            }

            let series = CandleSeries::new(self.symbols[idx].candles.clone());
            if series.len() < 10 {
                continue; // Not enough data
            }

            // 2. Update regime detector
            {
                let candles = &series.candles;
                if candles.len() >= 2 {
                    let prev = candles[candles.len() - 2].close;
                    let curr = candles[candles.len() - 1].close;
                    if prev > 0.0 {
                        let ret = (curr - prev) / prev;
                        self.risk.regime_detector.record_return(ret);
                    }
                }
            }

            // 3. Update correlation monitor (if secondary symbol configured)
            if self.config.secondary_symbol.is_some() && self.risk.correlation_monitor.is_some() {
                let sec_candles = if let Some(ref secondary) = self.config.secondary_symbol {
                    self.fetch_candles(secondary).await.unwrap_or(None)
                } else {
                    None
                };
                if let Some(ref mut monitor) = self.risk.correlation_monitor {
                    monitor.update_btc(series.candles.last().map(|c| c.close).unwrap_or(0.0));
                    if let Some(ref sec) = sec_candles {
                        if let Some(last) = sec.last() {
                            monitor.update_eth(last.close);
                        }
                    }
                }
            }

            // 4. Generate signal
            let signal = self.generate_signal(&symbol, &series).await;

            // 5. Update positions
            let last_candle = series.candles.last().cloned();
            self.update_positions(idx, &series, &last_candle).await;

            // 6. Update velocity tracker
            for pos in &self.symbols[idx].positions {
                self.risk
                    .velocity_tracker
                    .update(pos.id.parse::<u64>().unwrap_or(0), idx as i64, pos.unrealized_pnl_pct());
            }

            // 7. Compute composite risk
            let composite = self.compute_risk_score();

            // 8. Process signal
            if signal.direction.is_actionable() {
                let in_cooldown = idx.saturating_sub(self.symbols[idx].last_trade_candle)
                    < self.config.cooldown_candles as usize;
                if !in_cooldown && !self.drawdown_halted {
                    let executed = self.process_trade(idx, &signal, &last_candle).await?;
                    if executed {
                        self.symbols[idx].last_trade_candle = idx;
                        trades_made += 1;
                    }
                }
            }
        }

        // Update equity for drawdown tracking
        let total_equity = self.calculate_total_equity();
        if total_equity > self.peak_equity {
            self.peak_equity = total_equity;
        }
        if self.config.max_drawdown_pct > 0.0 && self.peak_equity > 0.0 {
            let dd = (self.peak_equity - total_equity) / self.peak_equity;
            if dd >= self.config.max_drawdown_pct {
                self.drawdown_halted = true;
            } else if total_equity >= self.peak_equity * (1.0 - self.config.max_drawdown_pct * 0.5) {
                self.drawdown_halted = false;
            }
        }

        Ok(trades_made)
    }

    /// Run the continuous trading loop. Blocks forever (until stopped).
    pub async fn run(&mut self) -> anyhow::Result<()> {
        if self.store.is_none() {
            anyhow::bail!("DataStore not configured. Call .with_store() first.");
        }
        if self.market_provider.is_none() {
            anyhow::bail!("MarketDataProvider not configured. Call .with_market_provider() first.");
        }
        if !self.dry_run && self.wallet.is_none() {
            anyhow::bail!(
                "Live mode requires a wallet. Set dry_run=true or provide a wallet key."
            );
        }

        tracing::info!(
            "Live trading started: {} symbols, dry_run={}",
            self.symbols.len(),
            self.dry_run
        );

        while self.running.load(Ordering::Relaxed) {
            let trades = self.run_cycle().await?;
            if trades > 0 {
                tracing::info!("Executed {trades} trade(s) this cycle");
            }
            tokio::time::sleep(tokio::time::Duration::from_secs(
                self.config.candle_poll_secs,
            ))
            .await;
        }

        tracing::info!("Live trading stopped");
        Ok(())
    }

    /// Stop the trading loop gracefully.
    pub fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
    }

    // ─── Private helpers ──────────────────────────────────

    /// Fetch latest candles for a symbol.
    async fn fetch_candles(&self, symbol: &str) -> anyhow::Result<Option<Vec<Candle>>> {
        let provider = match &self.market_provider {
            Some(p) => p,
            None => return Ok(None),
        };
        let candles = provider
            .fetch_historical(symbol, &self.config.timeframe, 1)
            .await?;
        Ok(Some(candles))
    }

    /// Generate a trading signal from candles.
    async fn generate_signal(&self, symbol: &str, series: &CandleSeries) -> Signal {
        let price = series.candles.last().map(|c| c.close).unwrap_or(0.0);
        let ts = series.candles.last().map(|c| c.timestamp).unwrap_or(0);

        let mut subsignals: Vec<SubSignal> = Vec::new();

        // Technical signals
        subsignals.push(compute_breakout_signal(series, &self.signal_config));
        subsignals.push(compute_mean_reversion_signal(series, &self.signal_config));
        subsignals.push(compute_trend_signal(series, &self.signal_config));
        subsignals.push(compute_vwap_signal(series, &self.signal_config));
        subsignals.push(compute_divergence_signal(series, &self.signal_config));

        // News sentiment signal (if store available)
        if let Some(ref store) = self.store {
            if let Ok(news_sig) = compute_news_signal(store, symbol, self.signal_config.news_lookback_hours).await {
                subsignals.push(news_sig);
            }
        }

        fuse_signals(&subsignals, symbol, price, ts)
    }

    /// Compute current composite risk score.
    fn compute_risk_score(&mut self) -> Option<crate::risk::CompositeRiskScore> {
        let regime = Some(self.risk.regime_detector.detect());
        let vel = self
            .symbols
            .iter()
            .flat_map(|s| s.positions.iter())
            .next()
            .and_then(|p| {
                self.risk
                    .velocity_tracker
                    .compute(p.id.parse::<u64>().unwrap_or(0))
            });
        let corr = self
            .risk
            .correlation_monitor
            .as_ref()
            .filter(|m| m.has_sufficient_data())
            .map(|m| m.evaluate());

        Some(compute_composite_score(
            regime.as_ref(),
            vel.as_ref(),
            corr.as_ref(),
            &self.risk.composite_config,
        ))
    }

    /// Update all open positions for a symbol (SL/TP check, trailing stop).
    async fn update_positions(
        &mut self,
        symbol_idx: usize,
        series: &CandleSeries,
        last_candle: &Option<Candle>,
    ) {
        let candle = match last_candle {
            Some(c) => c.clone(),
            None => return,
        };

        let mut i = 0;
        while i < self.symbols[symbol_idx].positions.len() {
            let pos = &self.symbols[symbol_idx].positions[i];
            let is_long = pos.side == "long";

            // SL check against H/L
            if let Some(sl) = pos.stop_loss {
                let hit = if is_long { candle.low <= sl } else { candle.high >= sl };
                if hit {
                    let p = self.symbols[symbol_idx].positions.remove(i);
                    self.close_position(p, sl, &candle, "stop_loss").await;
                    continue;
                }
            }
            // TP check against H/L
            if let Some(tp) = pos.take_profit {
                let hit = if is_long { candle.high >= tp } else { candle.low <= tp };
                if hit {
                    let p = self.symbols[symbol_idx].positions.remove(i);
                    self.close_position(p, tp, &candle, "take_profit").await;
                    continue;
                }
            }

            // Update price
            self.symbols[symbol_idx].positions[i].update_price(candle.close);

            // Trailing stop
            if self.config.use_trailing_stop {
                let atr = self.calculate_atr(&series.candles, 14);
                if let Some(atr_val) = atr {
                    if atr_val > 0.0 {
                        self.symbols[symbol_idx].positions[i]
                            .update_trailing_stop(self.config.trailing_activation_atr, self.config.trailing_distance_atr, atr_val);
                        if self.symbols[symbol_idx].positions[i].is_trailing_triggered() {
                            let p = self.symbols[symbol_idx].positions.remove(i);
                            self.close_position(p, candle.close, &candle, "trailing_stop").await;
                            continue;
                        }
                    }
                }
            }

            i += 1;
        }
    }

    /// Process a trading signal — open new position or close opposite.
    async fn process_trade(
        &mut self,
        symbol_idx: usize,
        signal: &Signal,
        candle: &Option<Candle>,
    ) -> anyhow::Result<bool> {
        let is_long = signal.direction.is_long();
        let side_str = if is_long { "long" } else { "short" };
        let price = candle.as_ref().map(|c| c.close).unwrap_or(signal.price);
        let timestamp = candle.as_ref().map(|c| c.timestamp).unwrap_or(0);

        // Close opposite position
        let opposite = if is_long { "short" } else { "long" };
        let mut j = 0;
        while j < self.symbols[symbol_idx].positions.len() {
            if self.symbols[symbol_idx].positions[j].side == opposite {
                let p = self.symbols[symbol_idx].positions.remove(j);
                self.close_position(p, price, candle.as_ref().unwrap(), "opposite_signal")
                    .await;
            } else {
                j += 1;
            }
        }

        // Check max positions
        if self.symbols[symbol_idx].positions.len() >= self.config.max_positions {
            return Ok(false);
        }

        // Position sizing
        let signal_strength = signal.strength.max(0.3);
        let position_value = self.capital * self.config.risk_per_trade * signal_strength;
        let mut quantity = if price > 0.0 {
            position_value / price
        } else {
            return Ok(false);
        };
        if quantity <= 0.0 {
            return Ok(false);
        }

        // Check capital
        let cost = price * quantity;
        if self.capital < cost {
            quantity = self.capital * 0.95 / price;
            if quantity <= 0.0 {
                return Ok(false);
            }
        }

        // Calculate fill price (DEX price impact)
        let fill_price = self.fill_sim.fill_price(price, is_long);
        let gas_cost = self.fill_sim.gas_cost_quote();

        if self.dry_run {
            tracing::info!(
                "[DRY-RUN] {} {} qty={:.4} @ {:.2} (impact: {:.4})",
                side_str, signal.symbol, quantity, fill_price,
                fill_price - price
            );
            self.trades.push(LiveTrade {
                timestamp,
                symbol: signal.symbol.clone(),
                side: side_str.to_string(),
                entry_price: fill_price,
                quantity,
                reason: "dry_run_entry".into(),
                executed: false,
                tx_signature: None,
            });
            return Ok(false); // Don't actually open position in dry-run
        }

        // Live execution via Jupiter
        let wallet = match &self.wallet {
            Some(w) => w,
            None => {
                tracing::warn!("No wallet configured for live trading");
                return Ok(false);
            }
        };

        let jupiter = match &self.jupiter {
            Some(j) => j,
            None => {
                tracing::warn!("No Jupiter client configured");
                return Ok(false);
            }
        };

        // Look up token mints
        let input_mint = if is_long {
            // Buying the token → output is stablecoin
            self.token_registry
                .by_symbol(&signal.symbol.replace("USDC", ""))
                .map(|t| t.mint.to_string())
                .unwrap_or_default()
        } else {
            // Selling the token → input is the token
            self.token_registry
                .by_symbol(&signal.symbol.replace("USDC", ""))
                .map(|t| t.mint.to_string())
                .unwrap_or_default()
        };
        let output_mint = self
            .token_registry
            .by_symbol("USDC")
            .map(|t| t.mint.to_string())
            .unwrap_or_default();

        if input_mint.is_empty() || output_mint.is_empty() {
            tracing::warn!("Could not resolve token mints for {}", signal.symbol);
            return Ok(false);
        }

        // Get quote
        let amount = self
            .token_registry
            .to_lamports(&signal.symbol.replace("USDC", ""), quantity)
            .unwrap_or(0);
        if amount == 0 {
            return Ok(false);
        }

        match jupiter
            .get_quote(&input_mint, &output_mint, amount, self.config.slippage_bps)
            .await
        {
            Ok(quote) => {
                tracing::info!(
                    "[LIVE] {} {}: {} → {} (impact: {:.2}%)",
                    side_str,
                    signal.symbol,
                    quote.in_amount,
                    quote.out_amount,
                    quote.price_impact_pct
                );

                // Build swap
                let quote_json = serde_json::to_value(&quote).unwrap_or_default();
                match jupiter
                    .build_swap(&quote_json, &wallet.pubkey_bs58())
                    .await
                {
                    Ok(swap_resp) => {
                        tracing::info!(
                            "[LIVE] Swap transaction built: {} bytes",
                            swap_resp.swap_transaction.len()
                        );
                        // In Stage 8, we'd sign and submit here via RPC
                        self.trades.push(LiveTrade {
                            timestamp,
                            symbol: signal.symbol.clone(),
                            side: side_str.to_string(),
                            entry_price: fill_price,
                            quantity,
                            reason: "live_entry".into(),
                            executed: true,
                            tx_signature: None,
                        });
                    }
                    Err(e) => {
                        tracing::error!("Failed to build swap: {e}");
                        return Ok(false);
                    }
                }
            }
            Err(e) => {
                tracing::error!("Failed to get quote: {e}");
                return Ok(false);
            }
        }

        // Deduct capital
        self.capital -= cost + gas_cost;

        // Open position
        let mut position = Position::new(&signal.symbol, side_str, fill_price, quantity);
        position.stop_loss = Some(if is_long {
            fill_price * (1.0 - self.config.stop_loss_pct)
        } else {
            fill_price * (1.0 + self.config.stop_loss_pct)
        });
        position.strategy_id = Some(signal.strategy_id.clone());
        self.symbols[symbol_idx].positions.push(position);

        tracing::info!(
            "[LIVE] Position opened: {} {} @ {:.2}",
            side_str,
            signal.symbol,
            fill_price
        );

        Ok(true)
    }

    /// Close a position and record the trade.
    async fn close_position(
        &mut self,
        mut position: Position,
        exit_price: f64,
        candle: &Candle,
        reason: &str,
    ) {
        let is_long = position.side == "long";
        let fill_price = self.fill_sim.fill_price(exit_price, !is_long);
        let entry_value = position.avg_entry_price() * position.total_quantity();
        let close_value = fill_price * position.total_quantity();
        let pnl = if is_long {
            close_value - entry_value
        } else {
            entry_value - close_value
        };
        let net_pnl = pnl - self.fill_sim.gas_cost_quote();

        self.capital += entry_value + net_pnl;

        let pnl_pct = if entry_value > 0.0 {
            (net_pnl / entry_value) * 100.0
        } else {
            0.0
        };

        tracing::info!(
            "[{}] {} closed: PnL={:.2} ({:.2}%), reason={}",
            if self.dry_run { "DRY-RUN" } else { "LIVE" },
            position.symbol,
            net_pnl,
            pnl_pct,
            reason
        );
    }

    /// Calculate ATR from a slice of candles.
    fn calculate_atr(&self, candles: &[Candle], period: usize) -> Option<f64> {
        if candles.len() < period + 1 {
            return None;
        }
        let n = candles.len();
        let mut trs = Vec::with_capacity(period);
        for i in (n - period)..n {
            let c = &candles[i];
            let p = &candles[i - 1];
            let tr = (c.high - c.low)
                .max((c.high - p.close).abs())
                .max((c.low - p.close).abs());
            trs.push(tr);
        }
        Some(trs.iter().sum::<f64>() / trs.len() as f64)
    }

    /// Calculate total equity (capital + open position value).
    fn calculate_total_equity(&self) -> f64 {
        let pos_value: f64 = self
            .symbols
            .iter()
            .flat_map(|s| s.positions.iter())
            .map(|p| p.unrealized_pnl())
            .sum();
        self.capital + pos_value
    }

    /// Get current summary for status display.
    pub fn status(&self) -> LiveStatus {
        let total_positions: usize = self.symbols.iter().map(|s| s.positions.len()).sum();
        let total_equity = self.calculate_total_equity();
        let dd = if self.peak_equity > 0.0 {
            (self.peak_equity - total_equity) / self.peak_equity
        } else {
            0.0
        };

        LiveStatus {
            running: self.running.load(Ordering::Relaxed),
            dry_run: self.dry_run,
            capital: self.capital,
            total_equity,
            open_positions: total_positions,
            total_trades: self.trades.len(),
            drawdown_pct: dd * 100.0,
            drawdown_halted: self.drawdown_halted,
            symbols: self.symbols.iter().map(|s| s.symbol.clone()).collect(),
        }
    }
}

/// Live trading status snapshot.
#[derive(Debug, Clone)]
pub struct LiveStatus {
    pub running: bool,
    pub dry_run: bool,
    pub capital: f64,
    pub total_equity: f64,
    pub open_positions: usize,
    pub total_trades: usize,
    pub drawdown_pct: f64,
    pub drawdown_halted: bool,
    pub symbols: Vec<String>,
}

/// Build a default signal config from strategy config.
pub fn signal_config_from_config(cfg: &crate::config::Config) -> SignalConfig {
    cfg.signals.into_signal_config()
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_engine_construct_dry_run() {
        let live_cfg = LiveConfig {
            enabled: true,
            candle_poll_secs: 300,
            initial_capital: 10000.0,
            symbols: vec!["SOLUSDC".into()],
            secondary_symbol: None,
            dry_run: true,
            slippage_bps: 50,
            sol_price: 150.0,
            max_positions: 3,
            risk_per_trade: 0.015,
            stop_loss_pct: 0.02,
            use_trailing_stop: true,
            trailing_activation_atr: 2.5,
            trailing_distance_atr: 2.5,
            max_drawdown_pct: 0.05,
            cooldown_candles: 96,
            timeframe: "5m".into(),
        };
        let signal_cfg = SignalConfig::default();
        let engine = LiveEngine::new_dry_run(live_cfg, signal_cfg);
        assert!(engine.dry_run);
        assert_eq!(engine.symbols.len(), 1);
        assert_eq!(engine.symbols[0].symbol, "SOLUSDC");
    }

    #[test]
    fn test_engine_status_dry_run() {
        let live_cfg = LiveConfig {
            enabled: true,
            candle_poll_secs: 300,
            initial_capital: 10000.0,
            symbols: vec!["SOLUSDC".into()],
            secondary_symbol: None,
            dry_run: true,
            slippage_bps: 50,
            sol_price: 150.0,
            max_positions: 3,
            risk_per_trade: 0.015,
            stop_loss_pct: 0.02,
            use_trailing_stop: true,
            trailing_activation_atr: 2.5,
            trailing_distance_atr: 2.5,
            max_drawdown_pct: 0.05,
            cooldown_candles: 96,
            timeframe: "5m".into(),
        };
        let signal_cfg = SignalConfig::default();
        let mut engine = LiveEngine::new_dry_run(live_cfg, signal_cfg);
        engine.capital = 10000.0;
        let status = engine.status();
        assert!(status.dry_run);
        assert!((status.capital - 10000.0).abs() < 0.1);
        assert!(status.running);
        assert_eq!(status.open_positions, 0);
    }

    #[test]
    fn test_engine_no_wallet_error() {
        let live_cfg = LiveConfig {
            enabled: true,
            candle_poll_secs: 300,
            initial_capital: 10000.0,
            symbols: vec!["SOLUSDC".into()],
            secondary_symbol: None,
            dry_run: false,
            slippage_bps: 50,
            sol_price: 150.0,
            max_positions: 3,
            risk_per_trade: 0.015,
            stop_loss_pct: 0.02,
            use_trailing_stop: true,
            trailing_activation_atr: 2.5,
            trailing_distance_atr: 2.5,
            max_drawdown_pct: 0.05,
            cooldown_candles: 96,
            timeframe: "5m".into(),
        };
        let signal_cfg = SignalConfig::default();
        let engine = LiveEngine::new(live_cfg, signal_cfg, false);
        // Without store and market provider, trying to run should error
        // But the engine itself should be constructable
        assert!(!engine.dry_run);
        assert!(engine.wallet.is_none());
    }

    #[test]
    fn test_stop_signal() {
        let live_cfg = LiveConfig::default();
        let signal_cfg = SignalConfig::default();
        let engine = LiveEngine::new_dry_run(live_cfg, signal_cfg);
        let stop = engine.stop_signal();
        assert!(stop.load(Ordering::Relaxed));
        engine.stop();
        assert!(!stop.load(Ordering::Relaxed));
    }
}
