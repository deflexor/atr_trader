/// Equity snapshots, trade PnL recording, and performance reporting.

use crate::live::state_manager::StateManager;
use crate::models::position::Position;

/// Tracks equity, trades, and performance statistics.
pub struct PnlTracker {
    state: StateManager,
    daily_start_equity: f64,
    total_pnl: f64,
    trade_count: u32,
    win_count: u32,
    trade_pnls: Vec<f64>,
}

impl PnlTracker {
    pub fn new(state: StateManager) -> Self {
        Self {
            state,
            daily_start_equity: 0.0,
            total_pnl: 0.0,
            trade_count: 0,
            win_count: 0,
            trade_pnls: Vec::new(),
        }
    }

    /// Record current equity state.
    pub async fn record_equity_snapshot(
        &self,
        positions: &[Position],
        cash: f64,
    ) {
        let upnl: f64 = positions.iter().map(|p| p.unrealized_pnl()).sum();
        let total_equity = cash + upnl;
        let daily_pnl = total_equity - self.daily_start_equity;

        let _ = self
            .state
            .save_equity_snapshot(
                total_equity,
                cash,
                upnl,
                positions.len() as i32,
                daily_pnl,
                self.total_pnl,
            )
            .await;
    }

    /// Record a closed trade.
    pub async fn record_trade_closed(
        &mut self,
        position: &Position,
        exit_price: f64,
        exit_reason: &str,
        commission: f64,
        slippage: f64,
    ) {
        let raw_pnl = calc_pnl(position.side.as_str(), position.avg_entry_price(), exit_price, position.total_quantity());
        let net_pnl = raw_pnl - commission - slippage;

        let trade = serde_json::json!({
            "id": uuid::Uuid::new_v4().to_string(),
            "position_id": position.id,
            "symbol": position.symbol,
            "side": position.side,
            "entry_price": position.avg_entry_price(),
            "exit_price": exit_price,
            "quantity": position.total_quantity(),
            "pnl": net_pnl,
            "commission": commission,
            "slippage": slippage,
            "exit_reason": exit_reason,
            "duration_seconds": 0,
            "context": {
                "highest_price_reached": position.highest_price,
                "trailing_stop_level": position.trailing_stop,
                "trailing_activated": position.trailing_activated,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
            },
            "created_at": position.created_at.to_rfc3339(),
            "closed_at": chrono::Utc::now().to_rfc3339(),
        });

        let _ = self.state.save_trade(&trade).await;

        self.total_pnl += net_pnl;
        self.trade_count += 1;
        self.trade_pnls.push(net_pnl);
        if net_pnl > 0.0 {
            self.win_count += 1;
        }
    }

    /// Reset daily PnL tracker.
    pub fn reset_daily(&mut self, equity: f64) {
        self.daily_start_equity = equity;
    }
}

/// Calculate raw PnL mirroring backtest engine logic.
fn calc_pnl(side: &str, entry_price: f64, exit_price: f64, qty: f64) -> f64 {
    if side == "long" {
        (exit_price - entry_price) * qty
    } else {
        (entry_price - exit_price) * qty
    }
}
