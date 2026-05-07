/// Position model for tracking open trades.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// A single entry (fill) within a position.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub price: f64,
    pub quantity: f64,
    pub timestamp: DateTime<Utc>,
}

/// Position model supporting pyramid entries and trailing stops.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub id: String,
    pub symbol: String,
    pub exchange: String,
    pub side: String, // "long" or "short"
    pub current_price: f64,
    pub entries: Vec<Entry>,
    pub stop_loss: Option<f64>,
    pub take_profit: Option<f64>,
    pub trailing_stop: Option<f64>,
    pub trailing_activated: bool,
    pub highest_price: f64,
    pub lowest_price: f64,
    pub trailing_atr_multiplier: f64,
    pub strategy_id: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

impl Position {
    /// Total cost basis across all entries.
    pub fn cost_basis(&self) -> f64 {
        self.entries.iter().map(|e| e.price * e.quantity).sum()
    }

    /// Total quantity across all entries.
    pub fn total_quantity(&self) -> f64 {
        self.entries.iter().map(|e| e.quantity).sum()
    }

    /// Average entry price weighted by quantity.
    pub fn avg_entry_price(&self) -> f64 {
        let qty = self.total_quantity();
        if qty == 0.0 {
            return 0.0;
        }
        self.cost_basis() / qty
    }

    /// Unrealized PnL based on current price.
    pub fn unrealized_pnl(&self) -> f64 {
        let qty = self.total_quantity();
        if self.side == "long" {
            (self.current_price - self.avg_entry_price()) * qty
        } else {
            (self.avg_entry_price() - self.current_price) * qty
        }
    }

    /// Add a new entry (fill) to the position.
    pub fn add_entry(&mut self, price: f64, quantity: f64) {
        self.entries.push(Entry {
            price,
            quantity,
            timestamp: Utc::now(),
        });
        self.updated_at = Utc::now();
    }

    /// Update current market price and track extremes for trailing stops.
    pub fn update_price(&mut self, price: f64) {
        self.current_price = price;
        if self.side == "long" {
            if price > self.highest_price {
                self.highest_price = price;
            }
        } else if price < self.lowest_price {
            self.lowest_price = price;
        }
        self.updated_at = Utc::now();
    }

    /// Update trailing stop based on ATR and price movement.
    pub fn update_trailing_stop(
        &mut self,
        activation_atr: f64,
        _distance_atr: f64,
        atr_value: f64,
    ) {
        if atr_value <= 0.0 {
            return;
        }

        let mult = if self.trailing_atr_multiplier > 0.0 {
            self.trailing_atr_multiplier
        } else {
            activation_atr
        };
        let activation_threshold = mult * atr_value;
        let trail_distance = mult * atr_value;

        if self.side == "long" {
            if !self.trailing_activated {
                if self.highest_price - self.avg_entry_price() >= activation_threshold {
                    self.trailing_activated = true;
                }
            }
            if self.trailing_activated {
                let new_trail = self.highest_price - trail_distance;
                if self.trailing_stop.is_none() || new_trail > self.trailing_stop.unwrap() {
                    self.trailing_stop = Some(new_trail);
                }
            }
        } else {
            if !self.trailing_activated {
                if self.avg_entry_price() - self.lowest_price >= activation_threshold {
                    self.trailing_activated = true;
                }
            }
            if self.trailing_activated {
                let new_trail = self.lowest_price + trail_distance;
                if self.trailing_stop.is_none() || new_trail < self.trailing_stop.unwrap() {
                    self.trailing_stop = Some(new_trail);
                }
            }
        }
    }

    /// Whether the trailing stop is triggered.
    pub fn is_trailing_triggered(&self) -> bool {
        if !self.trailing_activated {
            return false;
        }
        match self.trailing_stop {
            Some(ts) if self.side == "long" => self.current_price <= ts,
            Some(ts) => self.current_price >= ts,
            None => false,
        }
    }
}
