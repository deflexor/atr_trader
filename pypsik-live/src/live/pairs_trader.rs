/// Pairs trading loop — processes daily bars for a cointegrated pair.
///
/// Monitors two symbols on daily bars, computes z-score of log price ratio,
/// and executes paired entries/exits (long one leg, short the other).
///
/// Validated config: SHIB/UNI L30 E2.5 X0.0, +35.3% over 730d, Sharpe +1.67.

use std::collections::HashMap;

use tokio::sync::broadcast;
use tokio::time::{sleep, Duration};
use tracing::{info, warn};

use crate::exchange::client::ExchangeClient;
use crate::exchange::order_manager::OrderManager;
use crate::exchange::slippage_guard::SlippageGuard;
use crate::live::daily_feed::DailyFeed;
use crate::live::pnl_tracker::PnlTracker;
use crate::live::state_manager::StateManager;
use crate::models::pair::{PairDirection, PairSignal, PairState};
use crate::models::position::Position;
use crate::strategy::pairs_strategy::{PairsConfig, PairsStrategy};

use chrono::Utc;
use uuid::Uuid;

/// Commission rate: 0.036% maker per side (Bybit USDT perp, non-VIP)
const COMMISSION: f64 = 0.00036;
/// 4 sides per round-trip pair trade: entry (2 legs) + exit (2 legs)
const PAIR_COMMISSION: f64 = COMMISSION * 4.0;

/// Configuration for the pairs trader.
#[derive(Debug, Clone)]
pub struct PairsTraderConfig {
    pub api_key: String,
    pub api_secret: String,
    pub testnet: bool,
    pub market_type: String,
    pub leverage: u32,
    pub initial_capital: f64,
    pub pairs_config: PairsConfig,
    pub max_slippage_pct: f64,
    pub max_spread_pct: f64,
    pub limit_order_wait_seconds: u64,
    pub poll_interval_seconds: u64,
}

impl Default for PairsTraderConfig {
    fn default() -> Self {
        Self {
            api_key: String::new(),
            api_secret: String::new(),
            testnet: false,
            market_type: "perp".into(),
            leverage: 1,
            initial_capital: 1000.0,
            pairs_config: PairsConfig::shib_uni(1000.0),
            max_slippage_pct: 0.10,
            max_spread_pct: 0.15,
            limit_order_wait_seconds: 30,
            poll_interval_seconds: 3600, // 1 hour for daily bars
        }
    }
}

pub struct PairsTrader {
    config: PairsTraderConfig,
    strategy: PairsStrategy,
    exchange: ExchangeClient,
    state: StateManager,
    order_manager: OrderManager,
    daily_feed: DailyFeed,
    pnl_tracker: PnlTracker,
    capital: f64,
    positions: HashMap<String, Position>,
    pair_state: Option<PairState>,
    running: bool,
}

impl PairsTrader {
    pub fn new(config: PairsTraderConfig) -> Self {
        let pairs_cfg = config.pairs_config.clone();
        let capital = config.initial_capital;
        let strategy = PairsStrategy::new(pairs_cfg);

        let exchange = ExchangeClient::new(
            config.api_key.clone(),
            config.api_secret.clone(),
            config.testnet,
            &config.market_type,
            config.leverage,
        );

        let state = StateManager::new_placeholder();
        let guard = SlippageGuard::new(
            exchange.clone(),
            config.max_slippage_pct,
            config.max_spread_pct,
        );
        let order_manager = OrderManager::new(
            exchange.clone(),
            guard,
            state.clone(),
            config.limit_order_wait_seconds,
            COMMISSION,
        );
        let daily_feed = DailyFeed::new(
            exchange.clone(),
            state.clone(),
            &[
                strategy.config().symbol_a.clone(),
                strategy.config().symbol_b.clone(),
            ],
            strategy.config().lookback + 50,
        );
        let pnl_tracker = PnlTracker::new(state.clone());

        Self {
            config,
            strategy,
            exchange,
            state,
            order_manager,
            daily_feed,
            pnl_tracker,
            capital,
            positions: HashMap::new(),
            pair_state: None,
            running: false,
        }
    }

    /// Initialize all components: exchange, state DB, daily feed, restore state.
    pub async fn start(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        // Re-init state with real DB
        self.state = StateManager::init("data/live_trading.db").await?;

        // Re-create dependent components with real state
        let guard = SlippageGuard::new(
            self.exchange.clone(),
            self.config.max_slippage_pct,
            self.config.max_spread_pct,
        );
        self.order_manager = OrderManager::new(
            self.exchange.clone(),
            guard,
            self.state.clone(),
            self.config.limit_order_wait_seconds,
            COMMISSION,
        );
        self.daily_feed = DailyFeed::new(
            self.exchange.clone(),
            self.state.clone(),
            &[
                self.strategy.config().symbol_a.clone(),
                self.strategy.config().symbol_b.clone(),
            ],
            self.strategy.config().lookback + 50,
        );
        self.pnl_tracker = PnlTracker::new(self.state.clone());

        // Setup perp symbols
        self.exchange.start().await?;
        let symbols = vec![
            self.strategy.config().symbol_a.clone(),
            self.strategy.config().symbol_b.clone(),
        ];
        for symbol in &symbols {
            if let Err(e) = self.exchange.setup_perp_symbol(symbol).await {
                warn!(symbol = %symbol, error = %e, "perp_setup_failed");
            }
        }

        // Load daily history
        self.daily_feed.initialize().await?;

        // Restore pair state from DB
        self.restore_pair_state().await?;

        info!(
            pair = %self.strategy.config().pair_label,
            capital = self.capital,
            lookback = self.strategy.config().lookback,
            entry_z = self.strategy.config().entry_z,
            "pairs_trader.ready"
        );

        Ok(())
    }

    /// Main trading loop.
    pub async fn run(&mut self, mut shutdown: broadcast::Receiver<()>) {
        self.running = true;
        info!("pairs_trader.started");

        let poll = Duration::from_secs(self.config.poll_interval_seconds);

        while self.running {
            // Shutdown check
            match shutdown.try_recv() {
                Ok(_) | Err(broadcast::error::TryRecvError::Closed) => {
                    self.running = false;
                    break;
                }
                _ => {}
            }

            // Check for new daily bars
            let (updated, closes_a, closes_b) = self.daily_feed.check_for_new_bars().await;

            if updated {
                self.process_daily_tick(&closes_a, &closes_b).await;
            }

            // Sleep until next poll or shutdown
            tokio::select! {
                _ = sleep(poll) => {},
                _ = shutdown.recv() => {
                    self.running = false;
                }
            }
        }

        info!("pairs_trader.stopped");
    }

    /// Process a daily tick for the pair.
    async fn process_daily_tick(
        &mut self,
        closes_a: &[f64],
        closes_b: &[f64],
    ) {
        if closes_a.len() < self.strategy.config().lookback + 1
            || closes_b.len() < self.strategy.config().lookback + 1
        {
            return;
        }

        // Compute spread and z-score
        let spread = self.strategy.compute_spread(closes_a, closes_b);
        let zscores = self.strategy.rolling_zscore(&spread);

        let day_index = closes_a.len() - 1;
        let zscore = match zscores[day_index] {
            Some(z) => z,
            None => return,
        };
        let current_spread = spread[day_index];

        // Generate signal
        let in_position = self.pair_state.is_some();
        let signal = self.strategy.generate_signal(
            zscore,
            current_spread,
            day_index,
            in_position,
            self.pair_state.as_ref().map(|ps| ps.direction),
            self.pair_state.as_ref().map(|ps| ps.entry_day),
        );

        info!(
            pair = %self.strategy.config().pair_label,
            zscore = zscore,
            spread = current_spread,
            action = ?signal.action,
            in_position = in_position,
            "daily_tick"
        );

        match signal.action {
            crate::models::pair::PairAction::Enter => {
                self.execute_entry(&signal, &closes_a, &closes_b, day_index).await;
            }
            crate::models::pair::PairAction::ExitTakeProfit
            | crate::models::pair::PairAction::ExitStopLoss
            | crate::models::pair::PairAction::ExitMaxHold => {
                self.execute_exit(&signal).await;
            }
            crate::models::pair::PairAction::Hold => {
                // Update current prices for open positions
                self.update_position_prices(&closes_a, &closes_b).await;
            }
        }

        // Record equity snapshot
        self.record_equity().await;
    }

    /// Execute paired entry: open both legs simultaneously.
    async fn execute_entry(
        &mut self,
        signal: &PairSignal,
        _closes_a: &[f64],
        _closes_b: &[f64],
        day_index: usize,
    ) {
        let dir = signal.direction.unwrap();
        let cfg = self.strategy.config();

        // Get current prices
        let series_a = self.daily_feed.get_series(&cfg.symbol_a);
        let series_b = self.daily_feed.get_series(&cfg.symbol_b);

        let price_a = series_a.and_then(|s| s.candles.last().map(|c| c.close)).unwrap_or(0.0);
        let price_b = series_b.and_then(|s| s.candles.last().map(|c| c.close)).unwrap_or(0.0);

        if price_a <= 0.0 || price_b <= 0.0 {
            warn!("pairs_entry_skipped_no_price");
            return;
        }

        let qty_a = self.strategy.calculate_leg_size(price_a);
        let qty_b = self.strategy.calculate_leg_size(price_b);

        if qty_a <= 0.0 || qty_b <= 0.0 {
            warn!("pairs_entry_skipped_zero_quantity");
            return;
        }

        let side_a = dir.leg_side(true);
        let side_b = dir.leg_side(false);

        info!(
            pair = %cfg.pair_label,
            direction = ?dir,
            price_a = price_a,
            price_b = price_b,
            qty_a = qty_a,
            qty_b = qty_b,
            zscore = signal.zscore,
            "pair_entry"
        );

        // Place both entry orders (limit orders, sequential since OrderManager needs &mut)
        // Note: place_entry_order takes &mut self, so we can't use tokio::join!
        let result_a = self.order_manager.place_entry_order(
            &cfg.symbol_a,
            side_a,
            qty_a,
            price_a,
            "pair_entry",
        ).await;
        let result_b = self.order_manager.place_entry_order(
            &cfg.symbol_b,
            side_b,
            qty_b,
            price_b,
            "pair_entry",
        ).await;

        // Check fills
        let fill_a = if result_a.status == "filled" || result_a.status == "partial" {
            result_a
        } else {
            warn!(leg = "A", status = %result_a.status, "pair_entry_leg_a_failed");
            return;
        };
        let fill_b = if result_b.status == "filled" || result_b.status == "partial" {
            result_b
        } else {
            warn!(leg = "B", status = %result_b.status, "pair_entry_leg_b_failed");
            return;
        };

        let entry_price_a = fill_a.fill_price.unwrap_or(price_a);
        let entry_price_b = fill_b.fill_price.unwrap_or(price_b);
        let filled_qty_a = fill_a.filled_quantity;
        let filled_qty_b = fill_b.filled_quantity;

        // Create position objects
        let leg_a_id = Uuid::new_v4().to_string();
        let leg_b_id = Uuid::new_v4().to_string();

        let pos_a = Position {
            id: leg_a_id.clone(),
            symbol: cfg.symbol_a.clone(),
            exchange: "bybit".into(),
            side: side_a.to_string(),
            current_price: entry_price_a,
            entries: vec![crate::models::position::Entry {
                price: entry_price_a,
                quantity: filled_qty_a,
                timestamp: Utc::now(),
            }],
            stop_loss: None,
            take_profit: None,
            trailing_stop: None,
            trailing_activated: false,
            highest_price: if side_a == "long" { entry_price_a } else { 0.0 },
            lowest_price: if side_a == "short" { entry_price_a } else { f64::INFINITY },
            trailing_atr_multiplier: 2.5,
            strategy_id: Some("pairs".into()),
            created_at: Utc::now(),
            updated_at: Utc::now(),
        };

        let pos_b = Position {
            id: leg_b_id.clone(),
            symbol: cfg.symbol_b.clone(),
            exchange: "bybit".into(),
            side: side_b.to_string(),
            current_price: entry_price_b,
            entries: vec![crate::models::position::Entry {
                price: entry_price_b,
                quantity: filled_qty_b,
                timestamp: Utc::now(),
            }],
            stop_loss: None,
            take_profit: None,
            trailing_stop: None,
            trailing_activated: false,
            highest_price: if side_b == "long" { entry_price_b } else { 0.0 },
            lowest_price: if side_b == "short" { entry_price_b } else { f64::INFINITY },
            trailing_atr_multiplier: 2.5,
            strategy_id: Some("pairs".into()),
            created_at: Utc::now(),
            updated_at: Utc::now(),
        };

        // Deduct capital
        let entry_cost_a = entry_price_a * filled_qty_a;
        let entry_cost_b = entry_price_b * filled_qty_b;
        let commission_cost = (entry_cost_a + entry_cost_b) * COMMISSION;
        self.capital -= entry_cost_a + entry_cost_b + commission_cost;

        // Persist positions
        let _ = self.state.save_position(&pos_a).await;
        let _ = self.state.save_position(&pos_b).await;

        self.positions.insert(leg_a_id.clone(), pos_a);
        self.positions.insert(leg_b_id.clone(), pos_b);

        // Create and persist pair state
        let pair = PairState::new(
            &cfg.pair_label,
            &cfg.symbol_a,
            &cfg.symbol_b,
            dir,
            &leg_a_id,
            &leg_b_id,
            signal.zscore,
            signal.spread,
            day_index,
            entry_price_a,
            entry_price_b,
        );
        self.persist_pair_state(&pair).await;
        self.pair_state = Some(pair);

        info!(
            pair = %cfg.pair_label,
            leg_a_id = %leg_a_id,
            leg_b_id = %leg_b_id,
            entry_z = signal.zscore,
            "pair_opened"
        );
    }

    /// Execute paired exit: close both legs.
    async fn execute_exit(&mut self, signal: &PairSignal) {
        let pair = match self.pair_state.take() {
            Some(p) => p,
            None => {
                warn!("pair_exit_no_state");
                return;
            }
        };

        let exit_reason = match signal.action {
            crate::models::pair::PairAction::ExitTakeProfit => "take_profit",
            crate::models::pair::PairAction::ExitStopLoss => "stop_loss",
            crate::models::pair::PairAction::ExitMaxHold => "max_hold",
            _ => "unknown",
        };

        info!(
            pair = %pair.pair_label,
            reason = exit_reason,
            exit_z = signal.zscore,
            "pair_exit"
        );

        // Close both legs with market orders (parallel)
        let exit_a_side = if pair.direction.leg_side(true) == "long" { "sell" } else { "buy" };
        let exit_b_side = if pair.direction.leg_side(false) == "long" { "sell" } else { "buy" };

        // Get quantities from stored positions
        let qty_a = self.positions.get(&pair.leg_a_id).map(|p| p.total_quantity()).unwrap_or(0.0);
        let qty_b = self.positions.get(&pair.leg_b_id).map(|p| p.total_quantity()).unwrap_or(0.0);

        let (result_a, result_b) = tokio::join!(
            self.order_manager.place_exit_order(
                &pair.symbol_a,
                exit_a_side,
                qty_a,
                exit_reason,
            ),
            self.order_manager.place_exit_order(
                &pair.symbol_b,
                exit_b_side,
                qty_b,
                exit_reason,
            ),
        );

        let fill_a = &result_a;
        let fill_b = &result_b;

        let exit_price_a = fill_a.fill_price.unwrap_or(0.0);
        let exit_price_b = fill_b.fill_price.unwrap_or(0.0);

        // Calculate PnL
        let entry_price_a = pair.entry_price_a;
        let entry_price_b = pair.entry_price_b;

        let (pnl_a, pnl_b) = match pair.direction {
            PairDirection::LongAShortB => {
                let a = (exit_price_a - entry_price_a) / entry_price_a;
                let b = (entry_price_b - exit_price_b) / entry_price_b;
                (a, b)
            }
            PairDirection::ShortALongB => {
                let a = (entry_price_a - exit_price_a) / entry_price_a;
                let b = (exit_price_b - entry_price_b) / entry_price_b;
                (a, b)
            }
        };

        let gross_pnl_pct = (pnl_a + pnl_b) / 2.0;
        let net_pnl_pct = gross_pnl_pct - PAIR_COMMISSION;

        // Return capital
        let allocated = self.strategy.config().capital * self.strategy.config().position_pct * 2.0;
        let pnl_amount = allocated * net_pnl_pct;
        self.capital += allocated + pnl_amount;

        // Close positions in DB
        let _ = self.state.mark_position_closed(&pair.leg_a_id).await;
        let _ = self.state.mark_position_closed(&pair.leg_b_id).await;

        self.positions.remove(&pair.leg_a_id);
        self.positions.remove(&pair.leg_b_id);

        // Update pair state to closed
        let mut closed_pair = pair.clone();
        closed_pair.status = "closed".to_string();
        self.persist_pair_state(&closed_pair).await;

        // Record trades
        self.record_trade(&pair, exit_price_a, exit_price_b, pnl_a, pnl_b, exit_reason).await;

        info!(
            pair = %pair.pair_label,
            pnl_pct = net_pnl_pct * 100.0,
            pnl_amount = pnl_amount,
            reason = exit_reason,
            "pair_closed"
        );
    }

    /// Update current prices for open pair positions.
    async fn update_position_prices(&mut self, _closes_a: &[f64], _closes_b: &[f64]) {
        // Prices are already updated via the daily feed's latest bar
        // Just persist the state
        for pos in self.positions.values() {
            let _ = self.state.save_position(pos).await;
        }
    }

    // ── Persistence helpers ──────────────────────────────────────

    async fn restore_pair_state(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let json = self.state.get_state("pair_trade_active").await?;
        if let Some(json) = json {
            match serde_json::from_str::<PairState>(&json) {
                Ok(ps) if ps.status == "open" => {
                    info!(pair = %ps.pair_label, "pair_state_restored");
                    // Restore linked positions
                    if let Ok(positions) = self.state.load_open_positions().await {
                        for pos in positions {
                            self.positions.insert(pos.id.clone(), pos);
                        }
                    }
                    self.pair_state = Some(ps);
                }
                _ => {}
            }
        }
        Ok(())
    }

    async fn persist_pair_state(&self, pair: &PairState) {
        if let Ok(json) = serde_json::to_string(pair) {
            let key = if pair.status == "open" {
                "pair_trade_active"
            } else {
                "pair_trade_last"
            };
            let _ = self.state.set_state(key, &json).await;
        }
    }

    async fn record_trade(
        &self,
        pair: &PairState,
        exit_a: f64,
        exit_b: f64,
        pnl_a: f64,
        pnl_b: f64,
        reason: &str,
    ) {
        let cfg = self.strategy.config();
        let allocated = cfg.capital * cfg.position_pct;
        let _pnl_amount = allocated * (pnl_a + pnl_b) / 2.0;
        let commission_amount = allocated * PAIR_COMMISSION;

        // Record each leg as a trade
        for (symbol, side, entry, exit, qty) in &[
            (&pair.symbol_a, pair.direction.leg_side(true), pair.entry_price_a, exit_a,
             self.positions.get(&pair.leg_a_id).map(|p| p.total_quantity()).unwrap_or(0.0)),
            (&pair.symbol_b, pair.direction.leg_side(false), pair.entry_price_b, exit_b,
             self.positions.get(&pair.leg_b_id).map(|p| p.total_quantity()).unwrap_or(0.0)),
        ] {
            let trade = serde_json::json!({
                "id": Uuid::new_v4().to_string(),
                "position_id": pair.id,
                "symbol": symbol,
                "side": side,
                "entry_price": entry,
                "exit_price": exit,
                "quantity": qty,
                "pnl": pnl_a, // approximate — leg-level PnL
                "commission": commission_amount / 2.0,
                "slippage": 0.0,
                "exit_reason": reason,
                "context": serde_json::json!({
                    "pair_label": pair.pair_label,
                    "direction": pair.direction,
                    "entry_zscore": pair.entry_zscore,
                    "exit_zscore": 0.0,
                }),
            });
            let _ = self.state.save_trade(&trade).await;
        }
    }

    async fn record_equity(&self) {
        let open_positions = self.positions.len() as i32;
        let unrealized: f64 = self.positions.values().map(|p| p.unrealized_pnl()).sum();
        let _ = self.state.save_equity_snapshot(
            self.capital + unrealized,
            self.capital,
            unrealized,
            open_positions,
            0.0,
            0.0,
        ).await;
    }

    /// Graceful shutdown.
    pub async fn stop(&mut self) {
        self.running = false;

        // Save all open positions and pair state
        for pos in self.positions.values() {
            let _ = self.state.save_position(pos).await;
        }
        if let Some(ref pair) = self.pair_state {
            self.persist_pair_state(pair).await;
        }

        self.state.close().await;
        info!("pairs_trader.shutdown_complete");
    }
}
