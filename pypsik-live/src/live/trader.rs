/// Main live trading loop — orchestrates all components.
///
/// Replicates the Python LiveTrader: position management, signal processing,
/// trailing stops, anti-martingale sizing, and risk layer integration.

use std::collections::HashMap;

use tokio::sync::broadcast;
use tracing::{info, warn};
use uuid::Uuid;

use crate::exchange::client::ExchangeClient;
use crate::exchange::order_manager::OrderManager;
use crate::exchange::slippage_guard::SlippageGuard;
use crate::live::candle_feed::CandleFeed;
use crate::live::pnl_tracker::PnlTracker;
use crate::live::state_manager::StateManager;
use crate::models::candle::{Candle, CandleSeries};
use crate::models::position::Position;
use crate::models::signal::{Signal, SignalDirection};
use crate::risk::drawdown_budget::{DrawdownBudgetConfig, DrawdownBudgetTracker};
use crate::risk::regime_detector::RegimeDetector;
use crate::strategy::enhanced_signals::{generate_enhanced_signal, EnhancedSignalConfig};

/// Live trading configuration — mirrors Python LiveTradingConfig.
#[derive(Debug, Clone)]
pub struct LiveTradingConfig {
    pub api_key: String,
    pub api_secret: String,
    pub testnet: bool,
    pub market_type: String,
    pub leverage: u32,
    pub symbols: Vec<String>,
    pub initial_capital: f64,
    pub commission: f64,
    pub risk_per_trade: f64,
    pub max_positions: u32,
    pub timeframe: String,
    pub cooldown_candles: u32,
    pub max_slippage_pct: f64,
    pub max_spread_pct: f64,
    pub limit_order_wait_seconds: u64,
    pub use_trailing_stop: bool,
    pub trailing_activation_atr: f64,
    pub trailing_distance_atr: f64,
    pub use_atr_stops: bool,
    pub atr_period: usize,
    pub atr_sl_multiplier: f64,
    pub atr_tp_multiplier: f64,
    pub lookback_candles: usize,
    pub use_zero_drawdown_layer: bool,
    pub regime_lookback: usize,
    pub total_drawdown_budget: f64,
    pub per_trade_drawdown_budget: f64,
}

impl Default for LiveTradingConfig {
    fn default() -> Self {
        Self {
            api_key: String::new(),
            api_secret: String::new(),
            testnet: false,
            market_type: "perp".to_string(),
            leverage: 1,
            symbols: vec![
                "BTCUSDT".into(),
                "ETHUSDT".into(),
                "DOGEUSDT".into(),
                "TRXUSDT".into(),
                "SOLUSDT".into(),
                "ADAUSDT".into(),
                "AVAXUSDT".into(),
                "UNIUSDT".into(),
            ],
            initial_capital: 10000.0,
            commission: 0.0006,
            risk_per_trade: 0.03,
            max_positions: 2,
            timeframe: "5".to_string(),
            cooldown_candles: 96,
            max_slippage_pct: 0.10,
            max_spread_pct: 0.15,
            limit_order_wait_seconds: 30,
            use_trailing_stop: true,
            trailing_activation_atr: 2.0,
            trailing_distance_atr: 1.5,
            use_atr_stops: true,
            atr_period: 14,
            atr_sl_multiplier: 2.0,
            atr_tp_multiplier: 3.0,
            lookback_candles: 200,
            use_zero_drawdown_layer: true,
            regime_lookback: 100,
            total_drawdown_budget: 0.05,
            per_trade_drawdown_budget: 0.02,
        }
    }
}

/// Main live trading orchestrator.
pub struct LiveTrader {
    config: LiveTradingConfig,
    running: bool,
    capital: f64,
    positions: HashMap<String, Position>,
    anti_martingale_streak: u32,
    last_signal_candle: HashMap<String, u32>,
    candle_idx: HashMap<String, u32>,
    peak_equity: f64,
    signal_config: EnhancedSignalConfig,

    // Components (initialized in start())
    state_manager: Option<StateManager>,
    order_manager: Option<OrderManager>,
    candle_feed: Option<CandleFeed>,
    pnl_tracker: Option<PnlTracker>,
    regime_detector: Option<RegimeDetector>,
    budget_tracker: Option<DrawdownBudgetTracker>,
}

impl LiveTrader {
    pub fn new(config: LiveTradingConfig) -> Self {
        let signal_config = EnhancedSignalConfig::default();
        Self {
            config,
            running: false,
            capital: 0.0,
            positions: HashMap::new(),
            anti_martingale_streak: 0,
            last_signal_candle: HashMap::new(),
            candle_idx: HashMap::new(),
            peak_equity: 0.0,
            signal_config,
            state_manager: None,
            order_manager: None,
            candle_feed: None,
            pnl_tracker: None,
            regime_detector: None,
            budget_tracker: None,
        }
    }

    /// Initialize all components.
    pub async fn start(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let cfg = self.config.clone();
        self.capital = cfg.initial_capital;
        self.peak_equity = cfg.initial_capital;

        // State manager
        let state = StateManager::init("data/live_trading.db").await?;
        self.state_manager = Some(state.clone());

        // Exchange client
        let exchange = ExchangeClient::new(
            cfg.api_key.clone(),
            cfg.api_secret.clone(),
            cfg.testnet,
            &cfg.market_type,
            cfg.leverage,
        );
        exchange.start().await?;

        // Perp symbol setup
        if cfg.market_type == "perp" {
            for symbol in &cfg.symbols {
                if let Err(e) = exchange.setup_perp_symbol(symbol).await {
                    warn!(symbol = %symbol, error = %e, "perp_setup_failed");
                }
            }
        }

        // Slippage guard
        let guard = SlippageGuard::new(
            ExchangeClient::new(
                cfg.api_key.clone(),
                cfg.api_secret.clone(),
                cfg.testnet,
                &cfg.market_type,
                cfg.leverage,
            ),
            cfg.max_slippage_pct,
            cfg.max_spread_pct,
        );

        // Order manager
        let order_manager = OrderManager::new(
            exchange,
            guard,
            state.clone(),
            cfg.limit_order_wait_seconds,
            cfg.commission,
        );
        self.order_manager = Some(order_manager);

        // PnL tracker
        let pnl = PnlTracker::new(state.clone());
        self.pnl_tracker = Some(pnl);

        // Candle feed
        let mut feed = CandleFeed::new(
            ExchangeClient::new(
                cfg.api_key.clone(),
                cfg.api_secret.clone(),
                cfg.testnet,
                &cfg.market_type,
                cfg.leverage,
            ),
            state.clone(),
            &cfg.symbols,
            &cfg.timeframe,
            cfg.lookback_candles,
            &cfg.market_type,
        );
        feed.initialize().await;
        self.candle_feed = Some(feed);

        // Risk layer
        if cfg.use_zero_drawdown_layer {
            self.regime_detector = Some(RegimeDetector::new(cfg.regime_lookback));
            self.budget_tracker = Some(DrawdownBudgetTracker::new(
                DrawdownBudgetConfig {
                    total_budget_pct: cfg.total_drawdown_budget,
                    per_trade_budget_pct: cfg.per_trade_drawdown_budget,
                    ..Default::default()
                },
                cfg.initial_capital,
            ));
        }

        // Restore positions from DB
        self.restore_positions().await;
        self.reconcile_positions().await;

        info!(
            symbols = ?cfg.symbols,
            capital = self.capital,
            open_positions = self.positions.len(),
            "live_trader.ready"
        );

        Ok(())
    }

    /// Main trading loop.
    pub async fn run(
        &mut self,
        mut shutdown_rx: broadcast::Receiver<()>,
    ) {
        self.running = true;
        info!(symbols = ?self.config.symbols, "live_trader.started");

        while self.running {
            // Clone symbols to avoid borrow conflict
            let symbols = self.config.symbols.clone();
            // Process all symbols sequentially with staggered starts
            for (i, symbol) in symbols.iter().enumerate() {
                // Stagger
                if i > 0 {
                    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                }

                // Check shutdown
                match shutdown_rx.try_recv() {
                    Ok(_) | Err(tokio::sync::broadcast::error::TryRecvError::Closed) => {
                        self.running = false;
                        break;
                    }
                    _ => {}
                }

                if let Some(feed) = &mut self.candle_feed {
                    // Wait for new candle
                    let series = feed.wait_for_candle(symbol, &mut shutdown_rx, 310).await;
                    let series = match series {
                        Some(s) => s,
                        None => continue,
                    };

                    if let Some(candle) = series.candles.last() {
                        *self.candle_idx.entry(symbol.clone()).or_insert(0) += 1;

                        self.update_positions(symbol, candle, &series).await;

                        let signal = generate_enhanced_signal(symbol, &series, &self.signal_config);
                        if signal.is_actionable() && signal.direction != SignalDirection::Neutral {
                            let in_cooldown = self.in_cooldown(symbol);
                            if !in_cooldown {
                                self.process_signal(&signal, candle, &series).await;
                            }
                        }

                        // Record equity
                        let symbol_positions: Vec<&Position> = self
                            .positions
                            .values()
                            .filter(|p| p.symbol == *symbol)
                            .collect();
                        let positions_cloned: Vec<Position> =
                            symbol_positions.iter().map(|p| (*p).clone()).collect();
                        if let Some(pnl) = &self.pnl_tracker {
                            pnl.record_equity_snapshot(&positions_cloned, self.capital)
                                .await;
                        }

                        self.update_regime(candle);
                    }
                }
            }

            // Interruptible sleep
            if self.running {
                tokio::select! {
                    _ = tokio::time::sleep(std::time::Duration::from_secs(10)) => {},
                    _ = shutdown_rx.recv() => {
                        self.running = false;
                    }
                }
            }
        }
    }

    /// Signal shutdown.
    pub fn signal_shutdown(&mut self) {
        self.running = false;
        info!("live_trader.shutdown_signaled");
    }

    /// Graceful shutdown.
    pub async fn stop(&mut self) {
        self.running = false;
        info!("live_trader.stopping");

        if let Some(state) = &self.state_manager {
            for pos in self.positions.values() {
                if let Err(e) = state.save_position(pos).await {
                    warn!(symbol = %pos.symbol, error = %e, "save_position_failed");
                }
            }
            state.close().await;
        }

        info!(open_positions = self.positions.len(), "live_trader.stopped");
    }

    // ── Core methods mirroring backtest engine ────────────────

    async fn process_signal(
        &mut self,
        signal: &Signal,
        candle: &Candle,
        series: &CandleSeries,
    ) {
        let is_long = signal.direction == SignalDirection::Long;
        let side_str = if is_long { "long" } else { "short" };

        // Close opposite positions
        let opposite = if is_long { "short" } else { "long" };
        let opp_ids: Vec<String> = self
            .positions
            .values()
            .filter(|p| p.symbol == signal.symbol && p.side == opposite)
            .map(|p| p.id.clone())
            .collect();
        for id in opp_ids {
            if let Some(pos) = self.positions.get(&id) {
                let exit_price = candle.close;
                let pos_clone = pos.clone();
                self.close_position(&pos_clone, exit_price, "opposite_signal").await;
            }
        }

        // Check existing position on same side
        let has_existing = self
            .positions
            .values()
            .any(|p| p.symbol == signal.symbol && p.side == side_str);
        if has_existing {
            return;
        }

        // Check max positions
        let symbol_positions = self
            .positions
            .values()
            .filter(|p| p.symbol == signal.symbol)
            .count() as u32;
        if symbol_positions >= self.config.max_positions {
            return;
        }

        // Position sizing
        let signal_risk = signal.strength.max(0.3);
        let anti_mart_mult = (1.5_f64).min(1.0 + self.anti_martingale_streak as f64 * 0.1);
        let effective_risk = self.config.risk_per_trade * signal_risk * anti_mart_mult;
        let position_value = self.capital * effective_risk;
        let mut quantity = if signal.price > 0.0 {
            position_value / signal.price
        } else {
            0.0
        };

        if quantity <= 0.0 {
            return;
        }

        let max_affordable = if signal.price > 0.0 {
            self.capital * 0.95 / signal.price
        } else {
            0.0
        };
        if quantity > max_affordable {
            quantity = max_affordable;
        }
        if quantity <= 0.0 {
            return;
        }

        // Place entry order
        let side = if is_long { "buy" } else { "sell" };
        let result = if let Some(om) = &mut self.order_manager {
            om.place_entry_order(
                &signal.symbol,
                side,
                quantity,
                signal.price,
                "signal",
            )
            .await
        } else {
            return;
        };

        if result.status != "filled" && result.status != "partial" {
            warn!(status = %result.status, "entry_not_filled");
            return;
        }

        let fill_price = match result.fill_price {
            Some(p) => p,
            None => return,
        };
        let filled_qty = result.filled_quantity;
        if filled_qty <= 0.0 {
            return;
        }

        // ATR stops
        let atr = self.calculate_atr(series, self.config.atr_period);
        let stop_loss = atr.and_then(|a| {
            if a > 0.0 {
                if is_long {
                    Some(fill_price - a * self.config.atr_sl_multiplier)
                } else {
                    Some(fill_price + a * self.config.atr_sl_multiplier)
                }
            } else {
                None
            }
        });
        let take_profit = atr.and_then(|a| {
            if a > 0.0 {
                if is_long {
                    Some(fill_price + a * self.config.atr_tp_multiplier)
                } else {
                    Some(fill_price - a * self.config.atr_tp_multiplier)
                }
            } else {
                None
            }
        });

        // Create position
        let mut pos = Position {
            id: Uuid::new_v4().to_string(),
            symbol: signal.symbol.clone(),
            exchange: "bybit".to_string(),
            side: side_str.to_string(),
            current_price: fill_price,
            entries: Vec::new(),
            stop_loss,
            take_profit,
            trailing_stop: None,
            trailing_activated: false,
            highest_price: if is_long { fill_price } else { 0.0 },
            lowest_price: if !is_long { fill_price } else { f64::INFINITY },
            trailing_atr_multiplier: 2.5,
            strategy_id: Some("enhanced".to_string()),
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        };
        pos.add_entry(fill_price, filled_qty);

        let entry_cost = fill_price * filled_qty;
        let commission_cost = entry_cost * self.config.commission;
        self.capital -= entry_cost + commission_cost;

        if let Some(state) = &self.state_manager {
            let _ = state.save_position(&pos).await;
        }

        self.last_signal_candle.insert(
            signal.symbol.clone(),
            self.candle_idx.get(&signal.symbol).copied().unwrap_or(0),
        );
        self.positions.insert(pos.id.clone(), pos);

        info!(
            position_id = %self.positions.keys().last().unwrap_or(&String::new()),
            fill_price = format!("{:.2}", fill_price),
            quantity = format!("{:.6}", filled_qty),
            stop_loss = ?stop_loss.map(|s| format!("{:.2}", s)),
            take_profit = ?take_profit.map(|t| format!("{:.2}", t)),
            commission = format!("{:.6}", commission_cost),
            "position_opened"
        );
    }

    async fn update_positions(
        &mut self,
        symbol: &str,
        candle: &Candle,
        series: &CandleSeries,
    ) {
        let atr = self.calculate_atr(series, self.config.atr_period);
        let mut to_close: Vec<(String, f64, String)> = Vec::new();

        for pos in self.positions.values_mut() {
            if pos.symbol != symbol {
                continue;
            }

            let mut closed = false;

            if pos.side == "long" {
                if let Some(sl) = pos.stop_loss {
                    if candle.low <= sl {
                        to_close.push((pos.id.clone(), sl, "stop_loss".to_string()));
                        closed = true;
                    }
                }
                if !closed {
                    if let Some(tp) = pos.take_profit {
                        if candle.high >= tp {
                            to_close.push((pos.id.clone(), tp, "take_profit".to_string()));
                            closed = true;
                        }
                    }
                }
            } else {
                if let Some(sl) = pos.stop_loss {
                    if candle.high >= sl {
                        to_close.push((pos.id.clone(), sl, "stop_loss".to_string()));
                        closed = true;
                    }
                }
                if !closed {
                    if let Some(tp) = pos.take_profit {
                        if candle.low <= tp {
                            to_close.push((pos.id.clone(), tp, "take_profit".to_string()));
                            closed = true;
                        }
                    }
                }
            }

            if closed {
                continue;
            }

            pos.update_price(candle.close);

            if self.config.use_trailing_stop {
                if let Some(atr_val) = atr {
                    if atr_val > 0.0 {
                        pos.update_trailing_stop(
                            self.config.trailing_activation_atr,
                            self.config.trailing_distance_atr,
                            atr_val,
                        );
                        if pos.is_trailing_triggered() {
                            let ts = pos.trailing_stop.unwrap_or(candle.close);
                            to_close.push((pos.id.clone(), ts, "trailing_stop".to_string()));
                        }
                    }
                }
            }
        }

        // Close triggered positions
        for (id, exit_price, reason) in to_close {
            if let Some(pos) = self.positions.get(&id) {
                let pos_clone = pos.clone();
                self.close_position(&pos_clone, exit_price, &reason).await;
            }
        }
    }

    async fn close_position(
        &mut self,
        position: &Position,
        exit_price: f64,
        reason: &str,
    ) {
        let exit_side = if position.side == "long" { "sell" } else { "buy" };

        let result = if let Some(om) = &self.order_manager {
            om.place_exit_order(
                &position.symbol,
                exit_side,
                position.total_quantity(),
                reason,
            )
            .await
        } else {
            return;
        };

        let fill = if result.status == "filled" || result.status == "partial" {
            result.fill_price.unwrap_or(exit_price)
        } else {
            exit_price
        };

        let pnl = if position.side == "long" {
            (fill - position.avg_entry_price()) * position.total_quantity()
        } else {
            (position.avg_entry_price() - fill) * position.total_quantity()
        };

        let close_value = fill * position.total_quantity();
        let commission_cost = close_value * self.config.commission;
        let net_pnl = pnl - commission_cost;

        let entry_value = position.avg_entry_price() * position.total_quantity();
        self.capital += entry_value + net_pnl;

        if net_pnl > 0.0 {
            self.anti_martingale_streak += 1;
        } else {
            self.anti_martingale_streak = 0;
        }

        if let Some(pnl_tracker) = &mut self.pnl_tracker {
            pnl_tracker
                .record_trade_closed(position, fill, reason, commission_cost, 0.0)
                .await;
        }

        if let Some(state) = &self.state_manager {
            let _ = state.mark_position_closed(&position.id).await;
        }

        self.positions.remove(&position.id);

        info!(
            position_id = %position.id,
            fill = format!("{:.2}", fill),
            net_pnl = format!("{:.4}", net_pnl),
            commission = format!("{:.6}", commission_cost),
            streak = self.anti_martingale_streak,
            reason = reason,
            "position_closed"
        );
    }

    // ── Helpers ──────────────────────────────────────────────

    fn calculate_atr(&self, series: &CandleSeries, period: usize) -> Option<f64> {
        if series.candles.len() < period + 1 {
            return None;
        }

        let mut true_ranges = Vec::with_capacity(period);
        let n = series.candles.len();
        for i in (n - period)..n {
            let c = &series.candles[i];
            let prev = &series.candles[i - 1];
            let tr = (c.high - c.low)
                .abs()
                .max((c.high - prev.close).abs())
                .max((c.low - prev.close).abs());
            true_ranges.push(tr);
        }

        if true_ranges.is_empty() {
            return None;
        }
        Some(true_ranges.iter().sum::<f64>() / true_ranges.len() as f64)
    }

    fn calculate_equity(&self) -> f64 {
        let pos_value: f64 = self
            .positions
            .values()
            .map(|p| {
                if p.side == "long" {
                    p.current_price * p.total_quantity()
                } else {
                    (p.avg_entry_price() - p.current_price) * p.total_quantity()
                }
            })
            .sum();
        self.capital + pos_value
    }

    fn in_cooldown(&self, symbol: &str) -> bool {
        let last = self.last_signal_candle.get(symbol).copied().unwrap_or(0);
        let current = self.candle_idx.get(symbol).copied().unwrap_or(0);
        (current - last) < self.config.cooldown_candles
    }

    fn update_regime(&mut self, _candle: &Candle) {
        if let Some(detector) = &mut self.regime_detector {
            detector.update(0.0);
        }
        let equity = self.calculate_equity();
        self.peak_equity = self.peak_equity.max(equity);
        if let Some(tracker) = &mut self.budget_tracker {
            tracker.update_equity(equity, 0);
        }
    }

    async fn restore_positions(&mut self) {
        if let Some(state) = &self.state_manager {
            match state.load_open_positions().await {
                Ok(open_positions) => {
                    for pos in &open_positions {
                        self.capital -= pos.cost_basis();
                    }
                    if !open_positions.is_empty() {
                        info!(
                            count = open_positions.len(),
                            symbols = ?open_positions.iter().map(|p| p.symbol.clone()).collect::<Vec<_>>(),
                            "positions_restored"
                        );
                    }
                    for pos in open_positions {
                        self.positions.insert(pos.id.clone(), pos);
                    }
                }
                Err(e) => {
                    warn!(error = %e, "restore_positions_failed");
                }
            }
        }
    }

    async fn reconcile_positions(&mut self) {
        // We need a separate exchange client for reconciliation since order_manager owns one
        let exchange = ExchangeClient::new(
            self.config.api_key.clone(),
            self.config.api_secret.clone(),
            self.config.testnet,
            &self.config.market_type,
            self.config.leverage,
        );

        let known_symbols: Vec<String> = self
            .positions
            .values()
            .map(|p| p.symbol.clone())
            .collect();

        match exchange
            .fetch_exchange_positions(&self.config.symbols)
            .await
        {
            Ok(exchange_positions) => {
                let mut imported = Vec::new();
                for ep in exchange_positions {
                    if known_symbols.contains(&ep.symbol) {
                        continue;
                    }

                    let mut pos = Position {
                        id: Uuid::new_v4().to_string(),
                        symbol: ep.symbol.clone(),
                        exchange: "bybit".to_string(),
                        side: ep.side.clone(),
                        current_price: ep.entry_price,
                        entries: Vec::new(),
                        stop_loss: None,
                        take_profit: None,
                        trailing_stop: None,
                        trailing_activated: false,
                        highest_price: if ep.side == "long" {
                            ep.entry_price
                        } else {
                            0.0
                        },
                        lowest_price: if ep.side == "short" {
                            ep.entry_price
                        } else {
                            f64::INFINITY
                        },
                        trailing_atr_multiplier: 2.5,
                        strategy_id: Some("reconciled".to_string()),
                        created_at: chrono::Utc::now(),
                        updated_at: chrono::Utc::now(),
                    };
                    pos.add_entry(ep.entry_price, ep.quantity);

                    self.capital -= pos.cost_basis();
                    if let Some(state) = &self.state_manager {
                        let _ = state.save_position(&pos).await;
                    }
                    self.positions.insert(pos.id.clone(), pos);
                    imported.push(format!("{}:{}", ep.symbol, ep.side));
                }
                if !imported.is_empty() {
                    warn!(
                        count = imported.len(),
                        symbols = ?imported,
                        "positions_reconciled"
                    );
                }
            }
            Err(e) => {
                warn!(error = %e, "reconcile_positions_failed");
            }
        }
    }
}
