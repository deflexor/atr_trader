/// Async SQLite state persistence for live trading.
///
/// Uses rusqlite wrapped in `spawn_blocking` for non-blocking async access.

use rusqlite::Connection;
use std::path::Path;
use std::sync::Arc;
use tokio::sync::Mutex;
use tracing::info;

/// SQLite state manager for live trading persistence.
#[derive(Clone)]
pub struct StateManager {
    db: Arc<Mutex<Connection>>,
}

impl StateManager {
    /// Create and initialize the database.
    pub async fn init(db_path: &str) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let path = db_path.to_string();
        let conn = tokio::task::spawn_blocking(move || {
            if let Some(parent) = Path::new(&path).parent() {
                std::fs::create_dir_all(parent)?;
            }
            let conn = Connection::open(&path)?;
            conn.execute_batch(
                "
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    entries TEXT NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    trailing_stop REAL,
                    trailing_activated INTEGER DEFAULT 0,
                    highest_price REAL DEFAULT 0,
                    lowest_price REAL DEFAULT 999999999,
                    trailing_atr_multiplier REAL DEFAULT 2.5,
                    strategy_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    position_id TEXT,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    filled_quantity REAL DEFAULT 0,
                    avg_fill_price REAL,
                    signal_price REAL,
                    slippage_pct REAL,
                    commission REAL DEFAULT 0,
                    reason TEXT,
                    context TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    pnl REAL NOT NULL,
                    commission REAL NOT NULL,
                    slippage REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    duration_seconds INTEGER,
                    context TEXT,
                    created_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    total_equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    open_positions INTEGER NOT NULL,
                    daily_pnl REAL DEFAULT 0,
                    total_pnl REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                ",
            )?;
            Ok::<Connection, Box<dyn std::error::Error + Send + Sync>>(conn)
        })
        .await??;

        info!(db_path = db_path, "state_manager.initialized");
        Ok(Self {
            db: Arc::new(Mutex::new(conn)),
        })
    }

    /// Save (upsert) a position.
    pub async fn save_position(
        &self,
        pos: &crate::models::position::Position,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let pos = pos.clone();
        let db = self.db.clone();
        tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let entries_json = serde_json::to_string(&pos.entries)?;
            let lowest = if pos.lowest_price >= 999999999.0 {
                999999999.0
            } else {
                pos.lowest_price
            };
            conn.execute(
                "INSERT OR REPLACE INTO positions
                    (id, symbol, exchange, side, status, entries,
                     stop_loss, take_profit, trailing_stop, trailing_activated,
                     highest_price, lowest_price, trailing_atr_multiplier,
                     strategy_id, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
                rusqlite::params![
                    pos.id,
                    pos.symbol,
                    pos.exchange,
                    pos.side,
                    "open",
                    entries_json,
                    pos.stop_loss,
                    pos.take_profit,
                    pos.trailing_stop,
                    pos.trailing_activated as i32,
                    pos.highest_price,
                    lowest,
                    pos.trailing_atr_multiplier,
                    pos.strategy_id,
                    pos.created_at.to_rfc3339(),
                    pos.updated_at.to_rfc3339(),
                ],
            )?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        })
        .await??;
        Ok(())
    }

    /// Load all open positions.
    pub async fn load_open_positions(
        &self,
    ) -> Result<Vec<crate::models::position::Position>, Box<dyn std::error::Error + Send + Sync>>
    {
        let db = self.db.clone();
        let positions = tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let mut stmt = conn.prepare(
                "SELECT * FROM positions WHERE status = 'open'",
            )?;
            let rows = stmt.query_map([], |row| {
                let id: String = row.get("id")?;
                let symbol: String = row.get("symbol")?;
                let exchange: String = row.get("exchange")?;
                let side: String = row.get("side")?;
                let entries_json: String = row.get("entries")?;
                let stop_loss: Option<f64> = row.get("stop_loss")?;
                let take_profit: Option<f64> = row.get("take_profit")?;
                let trailing_stop: Option<f64> = row.get("trailing_stop")?;
                let trailing_activated: i32 = row.get("trailing_activated")?;
                let highest_price: f64 = row.get("highest_price")?;
                let lowest_price: f64 = row.get("lowest_price")?;
                let trailing_atr_multiplier: f64 = row.get("trailing_atr_multiplier")?;
                let strategy_id: Option<String> = row.get("strategy_id")?;
                let created_at: String = row.get("created_at")?;
                let updated_at: String = row.get("updated_at")?;

                Ok::<_, rusqlite::Error>((
                    id, symbol, exchange, side, entries_json, stop_loss, take_profit,
                    trailing_stop, trailing_activated, highest_price, lowest_price,
                    trailing_atr_multiplier, strategy_id, created_at, updated_at,
                ))
            })?;

            let mut result = Vec::new();
            for row in rows {
                let (
                    id, symbol, exchange, side, entries_json, stop_loss, take_profit,
                    trailing_stop, trailing_activated, highest_price, lowest_price,
                    trailing_atr_multiplier, strategy_id, created_at, updated_at,
                ) = row?;

                let entries: Vec<crate::models::position::Entry> =
                    serde_json::from_str(&entries_json).unwrap_or_default();

                let lowest = if lowest_price >= 999999999.0 {
                    f64::INFINITY
                } else {
                    lowest_price
                };

                result.push(crate::models::position::Position {
                    id,
                    symbol,
                    exchange,
                    side,
                    current_price: 0.0,
                    entries,
                    stop_loss,
                    take_profit,
                    trailing_stop,
                    trailing_activated: trailing_activated != 0,
                    highest_price,
                    lowest_price: lowest,
                    trailing_atr_multiplier,
                    strategy_id,
                    created_at: created_at.parse().unwrap_or_default(),
                    updated_at: updated_at.parse().unwrap_or_default(),
                });
            }
            Ok::<Vec<crate::models::position::Position>, Box<dyn std::error::Error + Send + Sync>>(result)
        })
        .await??;

        Ok(positions)
    }

    /// Mark a position as closed.
    pub async fn mark_position_closed(
        &self,
        position_id: &str,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let pid = position_id.to_string();
        let db = self.db.clone();
        tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                "UPDATE positions SET status = 'closed', updated_at = ?1 WHERE id = ?2",
                rusqlite::params![now, pid],
            )?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        })
        .await??;
        Ok(())
    }

    /// Save an order record.
    pub async fn save_order(
        &self,
        order: &serde_json::Value,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let order = order.clone();
        let db = self.db.clone();
        tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let now = chrono::Utc::now().to_rfc3339();
            let context = order.get("context").map(|c| c.to_string());
            conn.execute(
                "INSERT OR REPLACE INTO orders
                    (id, position_id, symbol, exchange, side, order_type,
                     quantity, price, status, filled_quantity, avg_fill_price,
                     signal_price, slippage_pct, commission, reason, context,
                     created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18)",
                rusqlite::params![
                    order["id"].as_str().unwrap_or(""),
                    order.get("position_id").and_then(|v| v.as_str()),
                    order["symbol"].as_str().unwrap_or(""),
                    order["exchange"].as_str().unwrap_or("bybit"),
                    order["side"].as_str().unwrap_or(""),
                    order["order_type"].as_str().unwrap_or(""),
                    order["quantity"].as_f64().unwrap_or(0.0),
                    order.get("price").and_then(|v| v.as_f64()),
                    order["status"].as_str().unwrap_or("pending"),
                    order["filled_quantity"].as_f64().unwrap_or(0.0),
                    order.get("avg_fill_price").and_then(|v| v.as_f64()),
                    order.get("signal_price").and_then(|v| v.as_f64()),
                    order.get("slippage_pct").and_then(|v| v.as_f64()),
                    order["commission"].as_f64().unwrap_or(0.0),
                    order.get("reason").and_then(|v| v.as_str()),
                    context,
                    order.get("created_at").and_then(|v| v.as_str()).unwrap_or(&now),
                    now,
                ],
            )?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        })
        .await??;
        Ok(())
    }

    /// Update order fill/status information.
    pub async fn update_order(
        &self,
        order_id: &str,
        status: &str,
        filled_quantity: f64,
        avg_fill_price: Option<f64>,
        slippage_pct: Option<f64>,
        commission: f64,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let oid = order_id.to_string();
        let st = status.to_string();
        let db = self.db.clone();
        tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                "UPDATE orders
                 SET status = ?1, filled_quantity = ?2, avg_fill_price = ?3,
                     slippage_pct = ?4, commission = ?5, updated_at = ?6
                 WHERE id = ?7",
                rusqlite::params![st, filled_quantity, avg_fill_price, slippage_pct, commission, now, oid],
            )?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        })
        .await??;
        Ok(())
    }

    /// Save a closed trade.
    pub async fn save_trade(
        &self,
        trade: &serde_json::Value,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let trade = trade.clone();
        let db = self.db.clone();
        tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let context = trade.get("context").map(|c| c.to_string());
            conn.execute(
                "INSERT OR REPLACE INTO trades
                    (id, position_id, symbol, side, entry_price, exit_price,
                     quantity, pnl, commission, slippage, exit_reason,
                     duration_seconds, context, created_at, closed_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
                rusqlite::params![
                    trade["id"].as_str().unwrap_or(""),
                    trade["position_id"].as_str().unwrap_or(""),
                    trade["symbol"].as_str().unwrap_or(""),
                    trade["side"].as_str().unwrap_or(""),
                    trade["entry_price"].as_f64().unwrap_or(0.0),
                    trade["exit_price"].as_f64().unwrap_or(0.0),
                    trade["quantity"].as_f64().unwrap_or(0.0),
                    trade["pnl"].as_f64().unwrap_or(0.0),
                    trade["commission"].as_f64().unwrap_or(0.0),
                    trade["slippage"].as_f64().unwrap_or(0.0),
                    trade["exit_reason"].as_str().unwrap_or(""),
                    trade.get("duration_seconds").and_then(|v| v.as_i64()),
                    context,
                    trade.get("created_at").and_then(|v| v.as_str()).unwrap_or(""),
                    trade.get("closed_at").and_then(|v| v.as_str()).unwrap_or(""),
                ],
            )?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        })
        .await??;
        Ok(())
    }

    /// Save an equity snapshot.
    pub async fn save_equity_snapshot(
        &self,
        total_equity: f64,
        cash: f64,
        unrealized_pnl: f64,
        open_positions: i32,
        daily_pnl: f64,
        total_pnl: f64,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let db = self.db.clone();
        tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                "INSERT INTO equity (timestamp, total_equity, cash, unrealized_pnl, open_positions, daily_pnl, total_pnl)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                rusqlite::params![now, total_equity, cash, unrealized_pnl, open_positions, daily_pnl, total_pnl],
            )?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        })
        .await??;
        Ok(())
    }

    /// Get recent equity snapshots for charting.
    pub async fn get_equity_history(
        &self,
        limit: usize,
    ) -> Result<Vec<serde_json::Value>, Box<dyn std::error::Error + Send + Sync>> {
        let db = self.db.clone();
        let rows = tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let mut stmt = conn.prepare(
                "SELECT * FROM equity ORDER BY id DESC LIMIT ?1",
            )?;
            let rows: Vec<serde_json::Value> = stmt
                .query_map(rusqlite::params![limit], |row| {
                    Ok(serde_json::json!({
                        "id": row.get::<_, i64>("id")?,
                        "timestamp": row.get::<_, String>("timestamp")?,
                        "total_equity": row.get::<_, f64>("total_equity")?,
                        "cash": row.get::<_, f64>("cash")?,
                        "unrealized_pnl": row.get::<_, f64>("unrealized_pnl")?,
                        "open_positions": row.get::<_, i32>("open_positions")?,
                        "daily_pnl": row.get::<_, f64>("daily_pnl")?,
                        "total_pnl": row.get::<_, f64>("total_pnl")?,
                    }))
                })?
                .filter_map(|r| r.ok())
                .collect();
            // Reverse to chronological order
            let mut rows = rows;
            rows.reverse();
            Ok::<Vec<serde_json::Value>, Box<dyn std::error::Error + Send + Sync>>(rows)
        })
        .await??;
        Ok(rows)
    }

    /// Get a state value by key.
    pub async fn get_state(
        &self,
        key: &str,
    ) -> Result<Option<String>, Box<dyn std::error::Error + Send + Sync>> {
        let key = key.to_string();
        let db = self.db.clone();
        let value = tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let mut stmt = conn.prepare("SELECT value FROM state WHERE key = ?1")?;
            let mut rows = stmt.query(rusqlite::params![key])?;
            match rows.next()? {
                Some(row) => Ok::<Option<String>, Box<dyn std::error::Error + Send + Sync>>(Some(row.get(0)?)),
                None => Ok(None),
            }
        })
        .await??;
        Ok(value)
    }

    /// Set a state value (upsert).
    pub async fn set_state(
        &self,
        key: &str,
        value: &str,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let key = key.to_string();
        let value = value.to_string();
        let db = self.db.clone();
        tokio::task::spawn_blocking(move || {
            let conn = db.blocking_lock();
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?1, ?2, ?3)",
                rusqlite::params![key, value, now],
            )?;
            Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
        })
        .await??;
        Ok(())
    }

    /// Close the database connection.
    pub async fn close(&self) {
        info!("state_manager.closed");
    }
}
