use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum OrderStatus {
    Pending,
    Open,
    Filled,
    PartialFilled,
    Cancelled,
    Rejected,
    Error,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum OrderSide {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum OrderType {
    Market,
    Limit,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Order {
    pub id: String,
    pub position_id: Option<String>,
    pub symbol: String,
    pub exchange: String,
    pub side: OrderSide,
    pub order_type: OrderType,
    pub quantity: f64,
    pub price: Option<f64>,
    pub filled_quantity: f64,
    pub avg_fill_price: Option<f64>,
    pub status: OrderStatus,
    pub slippage_pct: Option<f64>,
    pub commission: f64,
    pub reason: String,
    pub created_at: i64,
}

impl Order {
    pub fn new(symbol: &str, side: OrderSide, order_type: OrderType, quantity: f64) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            position_id: None,
            symbol: symbol.to_string(),
            exchange: "jupiter".into(),
            side,
            order_type,
            quantity,
            price: None,
            filled_quantity: 0.0,
            avg_fill_price: None,
            status: OrderStatus::Pending,
            slippage_pct: None,
            commission: 0.0,
            reason: String::new(),
            created_at: chrono::Utc::now().timestamp(),
        }
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_order_creation() {
        let o = Order::new("SOLUSDC", OrderSide::Buy, OrderType::Market, 10.0);
        assert_eq!(o.symbol, "SOLUSDC");
        assert_eq!(o.status, OrderStatus::Pending);
        assert!(o.filled_quantity.abs() < f64::EPSILON);
    }

    #[test]
    fn test_order_serde() {
        let o = Order::new("SOLUSDC", OrderSide::Sell, OrderType::Limit, 5.0);
        let json = serde_json::to_string(&o).unwrap();
        let back: Order = serde_json::from_str(&json).unwrap();
        assert_eq!(o.id, back.id);
        assert_eq!(o.quantity, back.quantity);
    }
}
