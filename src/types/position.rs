use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub price: f64,
    pub quantity: f64,
    pub timestamp: i64,
}

impl Entry {
    pub fn new(price: f64, quantity: f64) -> Self {
        Self {
            price,
            quantity,
            timestamp: chrono::Utc::now().timestamp(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Position {
    pub id: String,
    pub symbol: String,
    pub exchange: String,
    pub side: String,
    pub entries: Vec<Entry>,
    pub current_price: f64,
    pub stop_loss: Option<f64>,
    pub take_profit: Option<f64>,
    pub trailing_stop: Option<f64>,
    pub trailing_activated: bool,
    pub trailing_atr_multiplier: f64,
    pub created_at: i64,
    pub strategy_id: Option<String>,
    pub highest_price: f64,
    pub lowest_price: f64,
}

impl Position {
    pub fn new(symbol: &str, side: &str, price: f64, quantity: f64) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            symbol: symbol.to_string(),
            exchange: "jupiter".into(),
            side: side.to_string(),
            entries: vec![Entry::new(price, quantity)],
            current_price: price,
            stop_loss: None,
            take_profit: None,
            trailing_stop: None,
            trailing_activated: false,
            trailing_atr_multiplier: 2.5,
            created_at: chrono::Utc::now().timestamp(),
            strategy_id: None,
            highest_price: if side == "long" { price } else { 0.0 },
            lowest_price: if side == "short" { price } else { f64::MAX },
        }
    }

    pub fn total_quantity(&self) -> f64 {
        self.entries.iter().map(|e| e.quantity).sum()
    }

    pub fn cost_basis(&self) -> f64 {
        self.entries.iter().map(|e| e.price * e.quantity).sum()
    }

    pub fn avg_entry_price(&self) -> f64 {
        let qty = self.total_quantity();
        if qty == 0.0 {
            return 0.0;
        }
        self.cost_basis() / qty
    }

    pub fn unrealized_pnl(&self) -> f64 {
        if self.side == "long" {
            (self.current_price - self.avg_entry_price()) * self.total_quantity()
        } else {
            (self.avg_entry_price() - self.current_price) * self.total_quantity()
        }
    }

    pub fn unrealized_pnl_pct(&self) -> f64 {
        let basis = self.cost_basis();
        if basis == 0.0 {
            return 0.0;
        }
        self.unrealized_pnl() / basis * 100.0
    }

    pub fn update_price(&mut self, price: f64) {
        self.current_price = price;
        if self.side == "long" && price > self.highest_price {
            self.highest_price = price;
        } else if self.side == "short" && price < self.lowest_price {
            self.lowest_price = price;
        }
    }

    pub fn add_entry(&mut self, price: f64, quantity: f64) {
        self.entries.push(Entry::new(price, quantity));
    }

    pub fn reduce_entries(&mut self, fraction: f64) -> (f64, f64) {
        if fraction <= 0.0 || self.entries.is_empty() {
            return (0.0, 0.0);
        }
        let total_qty = self.total_quantity();
        let target_close = total_qty * fraction.min(1.0);
        let mut closed_qty = 0.0;
        let mut closed_value = 0.0;
        let mut remaining = target_close;

        while remaining > 0.0 && !self.entries.is_empty() {
            let entry = &mut self.entries[0];
            if entry.quantity <= remaining {
                closed_qty += entry.quantity;
                closed_value += entry.quantity * entry.price;
                remaining -= entry.quantity;
                self.entries.remove(0);
            } else {
                closed_qty += remaining;
                closed_value += remaining * entry.price;
                entry.quantity -= remaining;
                remaining = 0.0;
            }
        }
        (closed_qty, closed_value)
    }

    pub fn update_trailing_stop(&mut self, activation_atr: f64, _distance_atr: f64, atr_value: f64) {
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

    pub fn is_trailing_triggered(&self) -> bool {
        match self.trailing_stop {
            Some(stop) if self.trailing_activated => {
                if self.side == "long" {
                    self.current_price <= stop
                } else {
                    self.current_price >= stop
                }
            }
            _ => false,
        }
    }

    pub fn is_stop_triggered(&self) -> bool {
        match self.stop_loss {
            Some(sl) => {
                if self.side == "long" {
                    self.current_price <= sl
                } else {
                    self.current_price >= sl
                }
            }
            None => false,
        }
    }

    pub fn is_tp_triggered(&self) -> bool {
        match self.take_profit {
            Some(tp) => {
                if self.side == "long" {
                    self.current_price >= tp
                } else {
                    self.current_price <= tp
                }
            }
            None => false,
        }
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_position_long() {
        let mut p = Position::new("SOLUSDC", "long", 100.0, 10.0);
        assert_eq!(p.total_quantity(), 10.0);
        assert!((p.avg_entry_price() - 100.0).abs() < 0.001);

        p.update_price(110.0);
        assert!((p.unrealized_pnl() - 100.0).abs() < 0.001);
        assert!((p.unrealized_pnl_pct() - 10.0).abs() < 0.001);
    }

    #[test]
    fn test_position_short() {
        let mut p = Position::new("SOLUSDC", "short", 100.0, 5.0);
        p.update_price(90.0);
        assert!((p.unrealized_pnl() - 50.0).abs() < 0.001);
    }

    #[test]
    fn test_reduce_entries() {
        let mut p = Position::new("SOLUSDC", "long", 100.0, 10.0);
        p.add_entry(110.0, 10.0);
        assert_eq!(p.total_quantity(), 20.0);

        let (qty, val) = p.reduce_entries(0.5);
        assert!((qty - 10.0).abs() < 0.001);
        assert!((val - 1000.0).abs() < 0.001);
        assert_eq!(p.total_quantity(), 10.0);
    }

    #[test]
    fn test_trailing_stop_long() {
        let mut p = Position::new("SOLUSDC", "long", 100.0, 10.0);
        assert!(!p.is_trailing_triggered());

        // Price goes up enough to activate
        p.update_price(120.0);
        p.update_trailing_stop(2.0, 2.0, 5.0);
        assert!(p.trailing_activated);
        assert!(p.trailing_stop.is_some());
        // trail = 120 - 2.5*5 = 107.5 (uses trailing_atr_multiplier, not passed params)
        assert!((p.trailing_stop.unwrap() - 107.5).abs() < 0.001);
        assert!(!p.is_trailing_triggered());

        // Price drops below trail (107.5)
        p.update_price(107.0);
        assert!(p.is_trailing_triggered());
    }
}
