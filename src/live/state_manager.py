"""Async SQLite state persistence for live trading."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import structlog

from ..core.models.position import Position

logger = structlog.get_logger(__name__)

_SCHEMA_POSITIONS = """
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_SCHEMA_ORDERS = """
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
)
"""

_SCHEMA_TRADES = """
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
)
"""

_SCHEMA_EQUITY = """
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_equity REAL NOT NULL,
    cash REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    daily_pnl REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0
)
"""

_SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _serialize_entries(entries: list[dict]) -> str:
    """Convert entries list to JSON, coercing datetimes to ISO strings."""
    normalized = []
    for entry in entries:
        normalized.append({
            "price": entry["price"],
            "quantity": entry["quantity"],
            "timestamp": (
                entry["timestamp"].isoformat()
                if hasattr(entry["timestamp"], "isoformat")
                else entry["timestamp"]
            ),
        })
    return json.dumps(normalized)


def _deserialize_entries(raw: str) -> list[dict]:
    """Parse entries JSON back to list of dicts."""
    return json.loads(raw)


def _row_to_position(row: aiosqlite.Row) -> Position:
    """Convert a database row into a Position object."""
    lowest = row["lowest_price"]
    if lowest >= 999999999:
        lowest = float("inf")

    return Position(
        id=row["id"],
        symbol=row["symbol"],
        exchange=row["exchange"],
        side=row["side"],
        current_price=0.0,
        entries=_deserialize_entries(row["entries"]),
        stop_loss=row["stop_loss"],
        take_profit=row["take_profit"],
        trailing_stop=row["trailing_stop"],
        trailing_activated=bool(row["trailing_activated"]),
        highest_price=row["highest_price"],
        lowest_price=lowest,
        trailing_atr_multiplier=row["trailing_atr_multiplier"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class StateManager:
    """Async SQLite persistence for live trading state."""

    def __init__(self, db_path: str = "data/live_trading.db") -> None:
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init_db(self) -> None:
        """Open connection and create tables if not exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        for schema in (
            _SCHEMA_POSITIONS,
            _SCHEMA_ORDERS,
            _SCHEMA_TRADES,
            _SCHEMA_EQUITY,
            _SCHEMA_STATE,
        ):
            await self._db.execute(schema)
        await self._db.commit()
        logger.info("state_manager.initialized", db_path=self._db_path)

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("state_manager.closed")

    # ── Position operations ─────────────────────────────────────────

    async def save_position(self, position: Position) -> None:
        """Upsert position. Serialize entries list as JSON."""
        now = datetime.utcnow().isoformat()
        entries_json = _serialize_entries(position.entries)

        await self._db.execute(
            """
            INSERT OR REPLACE INTO positions
                (id, symbol, exchange, side, status, entries,
                 stop_loss, take_profit, trailing_stop, trailing_activated,
                 highest_price, lowest_price, trailing_atr_multiplier,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position.id,
                position.symbol,
                position.exchange,
                position.side,
                "open",
                entries_json,
                position.stop_loss,
                position.take_profit,
                position.trailing_stop,
                int(position.trailing_activated),
                position.highest_price,
                position.lowest_price if position.lowest_price != float("inf") else 999999999,
                position.trailing_atr_multiplier,
                position.created_at.isoformat() if hasattr(position.created_at, "isoformat") else str(position.created_at),
                now,
            ),
        )
        await self._db.commit()

    async def load_open_positions(self) -> list[Position]:
        """Load all open positions. Deserialize entries from JSON."""
        cursor = await self._db.execute(
            "SELECT * FROM positions WHERE status = 'open'"
        )
        rows = await cursor.fetchall()
        positions = [_row_to_position(row) for row in rows]
        logger.info(
            "state_manager.positions_loaded",
            count=len(positions),
        )
        return positions

    async def mark_position_closed(self, position_id: str) -> None:
        """Set status='closed', update updated_at."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            UPDATE positions
            SET status = 'closed', updated_at = ?
            WHERE id = ?
            """,
            (now, position_id),
        )
        await self._db.commit()
        logger.info("state_manager.position_closed", position_id=position_id)

    # ── Order operations ────────────────────────────────────────────

    async def save_order(self, order: dict) -> None:
        """Insert order record."""
        now = datetime.utcnow().isoformat()
        ctx = order.get("context")
        ctx_json = json.dumps(ctx) if ctx else None
        await self._db.execute(
            """
            INSERT OR REPLACE INTO orders
                (id, position_id, symbol, exchange, side, order_type,
                 quantity, price, status, filled_quantity, avg_fill_price,
                 signal_price, slippage_pct, commission, reason, context,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["id"],
                order.get("position_id"),
                order["symbol"],
                order["exchange"],
                order["side"],
                order["order_type"],
                order["quantity"],
                order.get("price"),
                order.get("status", "pending"),
                order.get("filled_quantity", 0),
                order.get("avg_fill_price"),
                order.get("signal_price"),
                order.get("slippage_pct"),
                order.get("commission", 0),
                order.get("reason"),
                ctx_json,
                order.get("created_at", now),
                now,
            ),
        )
        await self._db.commit()

    async def update_order(
        self,
        order_id: str,
        status: str,
        filled_quantity: float,
        avg_fill_price: Optional[float],
        slippage_pct: Optional[float],
        commission: float,
    ) -> None:
        """Update order fill/status information."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            UPDATE orders
            SET status = ?, filled_quantity = ?, avg_fill_price = ?,
                slippage_pct = ?, commission = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, filled_quantity, avg_fill_price, slippage_pct, commission, now, order_id),
        )
        await self._db.commit()

    # ── Trade operations ────────────────────────────────────────────

    async def save_trade(self, trade: dict) -> None:
        """Insert closed trade record."""
        now = datetime.utcnow().isoformat()
        ctx = trade.get("context")
        ctx_json = json.dumps(ctx) if ctx else None
        await self._db.execute(
            """
            INSERT OR REPLACE INTO trades
                (id, position_id, symbol, side, entry_price, exit_price,
                 quantity, pnl, commission, slippage, exit_reason,
                 duration_seconds, context, created_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade["id"],
                trade["position_id"],
                trade["symbol"],
                trade["side"],
                trade["entry_price"],
                trade["exit_price"],
                trade["quantity"],
                trade["pnl"],
                trade["commission"],
                trade["slippage"],
                trade["exit_reason"],
                trade.get("duration_seconds"),
                ctx_json,
                trade.get("created_at", now),
                trade.get("closed_at", now),
            ),
        )
        await self._db.commit()

    # ── Equity operations ───────────────────────────────────────────

    async def save_equity_snapshot(
        self,
        total_equity: float,
        cash: float,
        unrealized_pnl: float,
        open_positions: int,
        daily_pnl: float = 0,
        total_pnl: float = 0,
    ) -> None:
        """Record equity snapshot."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            INSERT INTO equity
                (timestamp, total_equity, cash, unrealized_pnl,
                 open_positions, daily_pnl, total_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, total_equity, cash, unrealized_pnl, open_positions, daily_pnl, total_pnl),
        )
        await self._db.commit()

    async def get_equity_history(self, limit: int = 1000) -> list[dict]:
        """Get recent equity snapshots for charting."""
        cursor = await self._db.execute(
            """
            SELECT * FROM equity ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    # ── Generic state ───────────────────────────────────────────────

    async def get_state(self, key: str) -> Optional[str]:
        """Get a state value by key."""
        cursor = await self._db.execute(
            "SELECT value FROM state WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_state(self, key: str, value: str) -> None:
        """Set a state value (upsert)."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            INSERT OR REPLACE INTO state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now),
        )
        await self._db.commit()
