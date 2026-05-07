/// Order lifecycle management — limit-order priority with market fallback.

use std::time::{Duration, Instant};

use tokio::time::sleep;
use tracing::{info, warn};
use uuid::Uuid;

use super::client::ExchangeClient;
use super::slippage_guard::SlippageGuard;
use crate::live::state_manager::StateManager;

const POLL_INTERVAL_SECS: u64 = 2;

/// Immutable result of an order operation.
#[derive(Debug, Clone)]
pub struct OrderResult {
    pub order_id: String,
    pub symbol: String,
    pub side: String,
    pub quantity: f64,
    pub filled_quantity: f64,
    pub fill_price: Option<f64>,
    pub slippage_pct: Option<f64>,
    pub status: String, // filled, partial, rejected, cancelled, error
    pub commission: f64,
}

/// Order placement with limit-order priority and slippage measurement.
pub struct OrderManager {
    client: ExchangeClient,
    guard: SlippageGuard,
    state: StateManager,
    limit_wait_secs: u64,
    commission_pct: f64,
}

impl OrderManager {
    pub fn new(
        client: ExchangeClient,
        guard: SlippageGuard,
        state: StateManager,
        limit_wait_secs: u64,
        commission_pct: f64,
    ) -> Self {
        Self {
            client,
            guard,
            state,
            limit_wait_secs,
            commission_pct,
        }
    }

    /// Place an entry order: check → limit → poll → market fallback.
    pub async fn place_entry_order(
        &mut self,
        symbol: &str,
        side: &str,
        quantity: f64,
        signal_price: f64,
        reason: &str,
    ) -> OrderResult {
        let order_id = Uuid::new_v4().to_string()[..16].to_string();

        // 1. Pre-trade slippage check
        let check = self.guard.check_orderbook(symbol, side, quantity).await;
        if !check.can_trade {
            info!(symbol = symbol, reason = %check.reason, "entry_rejected");
            return OrderResult {
                order_id,
                symbol: symbol.to_string(),
                side: side.to_string(),
                quantity,
                filled_quantity: 0.0,
                fill_price: None,
                slippage_pct: None,
                status: "rejected".to_string(),
                commission: 0.0,
            };
        }

        // 2. Place limit order at mid ± 2bp
        let limit_price = calc_limit_price(check.mid_price, side);
        let result = match self
            .client
            .place_limit_order(symbol, side, quantity, limit_price, false)
            .await
        {
            Ok(r) => r,
            Err(e) => {
                warn!(symbol = symbol, error = %e, "limit_order_failed");
                return OrderResult {
                    order_id,
                    symbol: symbol.to_string(),
                    side: side.to_string(),
                    quantity,
                    filled_quantity: 0.0,
                    fill_price: None,
                    slippage_pct: None,
                    status: "error".to_string(),
                    commission: 0.0,
                };
            }
        };

        let exchange_order_id = result.order_id;
        let limit_placed_at = Instant::now();

        // 3. Poll for fill
        let filled = self
            .poll_for_fill(symbol, &exchange_order_id)
            .await;
        let fill_latency = limit_placed_at.elapsed();

        match filled.status.as_str() {
            "filled" => {
                let fill_price = filled.avg_fill_price.unwrap_or(limit_price);
                let filled_qty = filled.filled;
                let slippage = measure_slippage(signal_price, fill_price, side);
                self.guard.record_actual_slippage(symbol, signal_price, fill_price);
                let commission = calc_commission(filled_qty, fill_price, self.commission_pct);

                info!(
                    symbol = symbol,
                    side = side,
                    fill_price = format!("{:.2}", fill_price),
                    slippage_pct = format!("{:.4}", slippage),
                    latency_ms = fill_latency.as_millis() as u64,
                    "limit_filled"
                );

                // Persist order
                let _ = self
                    .state
                    .save_order(&serde_json::json!({
                        "id": exchange_order_id,
                        "symbol": symbol,
                        "side": side,
                        "order_type": "limit",
                        "quantity": quantity,
                        "status": "filled",
                        "filled_quantity": filled_qty,
                        "avg_fill_price": fill_price,
                        "commission": commission,
                        "reason": reason,
                    }))
                    .await;

                OrderResult {
                    order_id: exchange_order_id,
                    symbol: symbol.to_string(),
                    side: side.to_string(),
                    quantity,
                    filled_quantity: filled_qty,
                    fill_price: Some(fill_price),
                    slippage_pct: Some(slippage),
                    status: "filled".to_string(),
                    commission,
                }
            }
            "partial" if filled.filled > 0.0 => {
                let _ = self.client.cancel_order(symbol, &exchange_order_id).await;
                let fill_price = filled.avg_fill_price.unwrap_or(limit_price);
                let filled_qty = filled.filled;
                let slippage = measure_slippage(signal_price, fill_price, side);
                self.guard.record_actual_slippage(symbol, signal_price, fill_price);
                let commission = calc_commission(filled_qty, fill_price, self.commission_pct);

                info!(
                    symbol = symbol,
                    side = side,
                    filled_qty = format!("{:.6}", filled_qty),
                    "limit_partial"
                );

                OrderResult {
                    order_id: exchange_order_id,
                    symbol: symbol.to_string(),
                    side: side.to_string(),
                    quantity,
                    filled_quantity: filled_qty,
                    fill_price: Some(fill_price),
                    slippage_pct: Some(slippage),
                    status: "partial".to_string(),
                    commission,
                }
            }
            _ => {
                // Timeout — cancel limit and fall back to market
                info!(symbol = symbol, "limit_timeout_fallback");
                let _ = self.client.cancel_order(symbol, &exchange_order_id).await;
                self.market_fallback(symbol, side, quantity, signal_price)
                    .await
            }
        }
    }

    /// Place an exit order — always market for speed.
    pub async fn place_exit_order(
        &self,
        symbol: &str,
        side: &str,
        quantity: f64,
        reason: &str,
    ) -> OrderResult {
        match self
            .client
            .place_market_order(symbol, side, quantity, true)
            .await
        {
            Ok(result) => {
                let fill_price = result.avg_fill_price;
                let filled_qty = result.quantity.unwrap_or(quantity);

                if fill_price.is_none() || filled_qty <= 0.0 {
                    return OrderResult {
                        order_id: result.order_id,
                        symbol: symbol.to_string(),
                        side: side.to_string(),
                        quantity,
                        filled_quantity: 0.0,
                        fill_price: None,
                        slippage_pct: None,
                        status: "error".to_string(),
                        commission: 0.0,
                    };
                }

                let fp = fill_price.unwrap();
                let commission = calc_commission(filled_qty, fp, self.commission_pct);

                info!(
                    symbol = symbol,
                    side = side,
                    fill_price = format!("{:.2}", fp),
                    commission = format!("{:.6}", commission),
                    "exit_filled"
                );

                let _ = self
                    .state
                    .save_order(&serde_json::json!({
                        "id": result.order_id,
                        "symbol": symbol,
                        "side": side,
                        "order_type": "market",
                        "quantity": quantity,
                        "status": "filled",
                        "filled_quantity": filled_qty,
                        "avg_fill_price": fp,
                        "commission": commission,
                        "reason": reason,
                    }))
                    .await;

                OrderResult {
                    order_id: result.order_id,
                    symbol: symbol.to_string(),
                    side: side.to_string(),
                    quantity,
                    filled_quantity: filled_qty,
                    fill_price: Some(fp),
                    slippage_pct: Some(0.0),
                    status: "filled".to_string(),
                    commission,
                }
            }
            Err(e) => {
                warn!(symbol = symbol, error = %e, "exit_error");
                OrderResult {
                    order_id: String::new(),
                    symbol: symbol.to_string(),
                    side: side.to_string(),
                    quantity,
                    filled_quantity: 0.0,
                    fill_price: None,
                    slippage_pct: None,
                    status: "error".to_string(),
                    commission: 0.0,
                }
            }
        }
    }

    /// Poll exchange for order status until filled or timed out.
    async fn poll_for_fill(
        &self,
        symbol: &str,
        order_id: &str,
    ) -> FillResult {
        let start = Instant::now();
        let timeout = Duration::from_secs(self.limit_wait_secs);

        loop {
            sleep(Duration::from_secs(POLL_INTERVAL_SECS)).await;

            match self.client.fetch_order_status(symbol, order_id).await {
                Ok(status) => {
                    let mapped = map_status(&status.status);
                    match mapped {
                        "filled" => {
                            return FillResult {
                                status: "filled".to_string(),
                                filled: status.filled,
                                avg_fill_price: status.avg_fill_price,
                            }
                        }
                        "partial" if status.filled > 0.0 => {
                            return FillResult {
                                status: "partial".to_string(),
                                filled: status.filled,
                                avg_fill_price: status.avg_fill_price,
                            }
                        }
                        "cancelled" | "rejected" => {
                            return FillResult {
                                status: mapped.to_string(),
                                filled: 0.0,
                                avg_fill_price: None,
                            }
                        }
                        _ => {}
                    }
                }
                Err(e) => {
                    warn!(symbol = symbol, error = %e, "poll_status_failed");
                }
            }

            if start.elapsed() >= timeout {
                return FillResult {
                    status: "timeout".to_string(),
                    filled: 0.0,
                    avg_fill_price: None,
                };
            }
        }
    }

    /// Fall back to market order after limit timeout.
    async fn market_fallback(
        &self,
        symbol: &str,
        side: &str,
        quantity: f64,
        signal_price: f64,
    ) -> OrderResult {
        match self
            .client
            .place_market_order(symbol, side, quantity, false)
            .await
        {
            Ok(result) => {
                let fill_price = result.avg_fill_price;
                let filled_qty = result.quantity.unwrap_or(quantity);

                if fill_price.is_none() || filled_qty <= 0.0 {
                    return OrderResult {
                        order_id: result.order_id,
                        symbol: symbol.to_string(),
                        side: side.to_string(),
                        quantity,
                        filled_quantity: 0.0,
                        fill_price: None,
                        slippage_pct: None,
                        status: "error".to_string(),
                        commission: 0.0,
                    };
                }

                let fp = fill_price.unwrap();
                let slippage = measure_slippage(signal_price, fp, side);
                let commission = calc_commission(filled_qty, fp, self.commission_pct);

                info!(
                    symbol = symbol,
                    side = side,
                    fill_price = format!("{:.2}", fp),
                    slippage_pct = format!("{:.4}", slippage),
                    "market_fallback_filled"
                );

                OrderResult {
                    order_id: result.order_id,
                    symbol: symbol.to_string(),
                    side: side.to_string(),
                    quantity,
                    filled_quantity: filled_qty,
                    fill_price: Some(fp),
                    slippage_pct: Some(slippage),
                    status: "filled".to_string(),
                    commission,
                }
            }
            Err(e) => {
                warn!(symbol = symbol, error = %e, "market_fallback_failed");
                OrderResult {
                    order_id: String::new(),
                    symbol: symbol.to_string(),
                    side: side.to_string(),
                    quantity,
                    filled_quantity: 0.0,
                    fill_price: None,
                    slippage_pct: None,
                    status: "error".to_string(),
                    commission: 0.0,
                }
            }
        }
    }
}

struct FillResult {
    status: String,
    filled: f64,
    avg_fill_price: Option<f64>,
}

/// Calculate limit price with 2bp offset from mid.
fn calc_limit_price(mid_price: f64, side: &str) -> f64 {
    let offset = mid_price * 0.0002; // 2 basis points
    if side == "buy" {
        mid_price - offset
    } else {
        mid_price + offset
    }
}

/// Map Bybit order status to normalized status.
fn map_status(raw: &str) -> &str {
    match raw {
        "Filled" => "filled",
        "PartiallyFilled" => "partial",
        "Cancelled" => "cancelled",
        "Rejected" => "rejected",
        "New" | "Created" => "open",
        _ => "open",
    }
}

/// Calculate commission on a fill.
fn calc_commission(filled_qty: f64, fill_price: f64, commission_pct: f64) -> f64 {
    filled_qty * fill_price * commission_pct
}

/// Measure slippage percentage (signed).
fn measure_slippage(signal_price: f64, fill_price: f64, side: &str) -> f64 {
    if signal_price <= 0.0 {
        return 0.0;
    }
    if side == "buy" {
        (fill_price - signal_price) / signal_price * 100.0
    } else {
        (signal_price - fill_price) / signal_price * 100.0
    }
}
