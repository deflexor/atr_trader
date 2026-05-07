/// Pre-trade orderbook slippage estimation with adaptive sizing.

use std::collections::VecDeque;

use crate::exchange::client::ExchangeClient;
use tracing::warn;

const MAX_LEVELS_TO_WALK: usize = 3;
const SPREAD_HISTORY_SIZE: usize = 10;

/// Immutable result of an orderbook slippage check.
#[derive(Debug, Clone)]
pub struct SlippageCheck {
    pub symbol: String,
    pub side: String,
    pub mid_price: f64,
    pub expected_fill_price: f64,
    pub expected_slippage_pct: f64,
    pub spread_pct: f64,
    pub can_trade: bool,
    pub reason: String,
}

/// Pre-trade slippage estimation with adaptive sizing.
pub struct SlippageGuard {
    client: ExchangeClient,
    pub max_slippage_pct: f64,
    pub max_spread_pct: f64,
    rolling_window: usize,
    blacklist_threshold: usize,
    spread_history: std::collections::HashMap<String, VecDeque<f64>>,
    slippage_history: std::collections::HashMap<String, VecDeque<f64>>,
}

impl SlippageGuard {
    pub fn new(
        client: ExchangeClient,
        max_slippage_pct: f64,
        max_spread_pct: f64,
    ) -> Self {
        Self {
            client,
            max_slippage_pct,
            max_spread_pct,
            rolling_window: 20,
            blacklist_threshold: 3,
            spread_history: std::collections::HashMap::new(),
            slippage_history: std::collections::HashMap::new(),
        }
    }

    /// Analyze orderbook before placing an order.
    pub async fn check_orderbook(
        &mut self,
        symbol: &str,
        side: &str,
        quantity: f64,
    ) -> SlippageCheck {
        if side != "buy" && side != "sell" {
            return self.reject(symbol, side, 0.0, 0.0, &format!("invalid side: {}", side));
        }

        let book = match self.client.fetch_orderbook(symbol, Some(10)).await {
            Ok(b) => b,
            Err(e) => {
                warn!(symbol = symbol, error = %e, "orderbook_fetch_failed");
                return self.reject(symbol, side, 0.0, 0.0, "orderbook fetch failed");
            }
        };

        if book.bids.is_empty() || book.asks.is_empty() {
            return self.reject(symbol, side, 0.0, 0.0, "empty orderbook");
        }

        let best_bid = book.bids[0][0];
        let best_ask = book.asks[0][0];
        let mid_price = (best_bid + best_ask) / 2.0;
        let spread_pct = if mid_price > 0.0 {
            (best_ask - best_bid) / mid_price * 100.0
        } else {
            0.0
        };

        self.record_spread(symbol, spread_pct);

        if spread_pct > self.max_spread_pct {
            return SlippageCheck {
                symbol: symbol.to_string(),
                side: side.to_string(),
                mid_price,
                expected_fill_price: 0.0,
                expected_slippage_pct: 0.0,
                spread_pct,
                can_trade: false,
                reason: format!("spread {:.4}% > max {:.4}%", spread_pct, self.max_spread_pct),
            };
        }

        let levels: &[[f64; 2]] = if side == "buy" { &book.asks } else { &book.bids };
        let (fill_price, available) = walk_book_side(levels, quantity);

        let expected_slippage = calc_slippage_pct(fill_price, mid_price, side);

        if expected_slippage > self.max_slippage_pct {
            return SlippageCheck {
                symbol: symbol.to_string(),
                side: side.to_string(),
                mid_price,
                expected_fill_price: fill_price,
                expected_slippage_pct: expected_slippage,
                spread_pct,
                can_trade: false,
                reason: format!(
                    "expected slippage {:.4}% > max {:.4}%",
                    expected_slippage, self.max_slippage_pct
                ),
            };
        }

        if available < quantity {
            return SlippageCheck {
                symbol: symbol.to_string(),
                side: side.to_string(),
                mid_price,
                expected_fill_price: fill_price,
                expected_slippage_pct: expected_slippage,
                spread_pct,
                can_trade: false,
                reason: format!(
                    "insufficient liquidity: {:.6} < {:.6}",
                    available, quantity
                ),
            };
        }

        SlippageCheck {
            symbol: symbol.to_string(),
            side: side.to_string(),
            mid_price,
            expected_fill_price: fill_price,
            expected_slippage_pct: expected_slippage,
            spread_pct,
            can_trade: true,
            reason: String::new(),
        }
    }

    /// Record actual slippage after fill.
    pub fn record_actual_slippage(
        &mut self,
        symbol: &str,
        signal_price: f64,
        fill_price: f64,
    ) -> f64 {
        if signal_price <= 0.0 {
            return 0.0;
        }
        let slippage_pct = (fill_price - signal_price).abs() / signal_price * 100.0;
        let history = self
            .slippage_history
            .entry(symbol.to_string())
            .or_insert_with(|| VecDeque::with_capacity(self.rolling_window));
        history.push_back(slippage_pct);
        if history.len() > self.rolling_window {
            history.pop_front();
        }
        slippage_pct
    }

    fn reject(
        &self,
        symbol: &str,
        side: &str,
        mid: f64,
        spread: f64,
        reason: &str,
    ) -> SlippageCheck {
        SlippageCheck {
            symbol: symbol.to_string(),
            side: side.to_string(),
            mid_price: mid,
            expected_fill_price: 0.0,
            expected_slippage_pct: 0.0,
            spread_pct: spread,
            can_trade: false,
            reason: reason.to_string(),
        }
    }

    fn record_spread(&mut self, symbol: &str, spread_pct: f64) {
        let history = self
            .spread_history
            .entry(symbol.to_string())
            .or_insert_with(|| VecDeque::with_capacity(SPREAD_HISTORY_SIZE));
        history.push_back(spread_pct);
        if history.len() > SPREAD_HISTORY_SIZE {
            history.pop_front();
        }
    }
}

/// Walk top N levels of the book and return (weighted_fill_price, total_available).
fn walk_book_side(levels: &[[f64; 2]], quantity: f64) -> (f64, f64) {
    let mut cost = 0.0;
    let mut filled = 0.0;
    for [price, qty] in levels.iter().take(MAX_LEVELS_TO_WALK) {
        let take = (quantity - filled).min(*qty);
        cost += price * take;
        filled += take;
        if filled >= quantity {
            break;
        }
    }
    if filled == 0.0 {
        (0.0, 0.0)
    } else {
        (cost / filled, filled)
    }
}

/// Calculate slippage percentage (always positive).
fn calc_slippage_pct(fill_price: f64, mid_price: f64, side: &str) -> f64 {
    if mid_price <= 0.0 {
        return 0.0;
    }
    if side == "buy" {
        (fill_price - mid_price) / mid_price * 100.0
    } else {
        (mid_price - fill_price) / mid_price * 100.0
    }
}
