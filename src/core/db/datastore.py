"""Data storage for historical candles using SQLite and CSV.

Provides persistence for historical data to enable longer backtesting periods.
"""

from __future__ import annotations

import asyncio
import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

from ...core.models.candle import Candle

logger = logging.getLogger(__name__)


class DataStore:
    """Persistent storage for historical candle data.

    Uses SQLite as the primary store and CSV for export/backup.
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "candles.db"
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite database schema."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                UNIQUE(symbol, exchange, timeframe, timestamp)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_candle_lookup
            ON candles(symbol, exchange, timeframe, timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_candle_period
            ON candles(timestamp)
        """)
        conn.commit()
        conn.close()

    def save_candles(self, candles: list[Candle]) -> int:
        """Save candles to the database using batch insert.

        Args:
            candles: List of Candle objects to save

        Returns:
            Number of candles saved
        """
        if not candles:
            return 0

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Batch insert using executemany with transaction
        rows = [
            (
                c.symbol,
                c.exchange,
                c.timeframe,
                int(c.timestamp.timestamp()),
                c.open,
                c.high,
                c.low,
                c.close,
                c.volume,
            )
            for c in candles
        ]

        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.executemany(
                """
                INSERT OR IGNORE INTO candles
                (symbol, exchange, timeframe, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            inserted = cursor.rowcount
        except Exception as e:
            conn.rollback()
            logger.debug(f"Batch insert failed: {e}")
            inserted = 0

        conn.close()
        logger.info(f"Saved {inserted} new candles to {self.db_path}")
        return inserted

    def get_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Candle]:
        """Retrieve candles from the database.

        Args:
            symbol: Trading pair symbol
            exchange: Exchange name
            timeframe: Candle timeframe
            start_timestamp: Start timestamp (Unix seconds)
            end_timestamp: End timestamp (Unix seconds)
            limit: Maximum number of candles to return

        Returns:
            List of Candle objects sorted oldest-first
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM candles WHERE symbol = ? AND exchange = ? AND timeframe = ?"
        params = [symbol, exchange, timeframe]

        if start_timestamp:
            query += " AND timestamp >= ?"
            params.append(start_timestamp)
        if end_timestamp:
            query += " AND timestamp <= ?"
            params.append(end_timestamp)

        query += " ORDER BY timestamp ASC"

        if limit:
            query += f" LIMIT {limit}"

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        candles = []
        for row in rows:
            candles.append(
                Candle(
                    symbol=row["symbol"],
                    exchange=row["exchange"],
                    timeframe=row["timeframe"],
                    timestamp=datetime.fromtimestamp(row["timestamp"]),
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                )
            )

        return candles

    def get_candle_count(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
    ) -> int:
        """Get count of candles in database for a symbol/timeframe."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM candles
            WHERE symbol = ? AND exchange = ? AND timeframe = ?
        """,
            (symbol, exchange, timeframe),
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_date_range(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
    ) -> tuple[Optional[int], Optional[int]]:
        """Get the earliest and latest timestamps in the database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT MIN(timestamp), MAX(timestamp) FROM candles
            WHERE symbol = ? AND exchange = ? AND timeframe = ?
        """,
            (symbol, exchange, timeframe),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0], row[1]

    def export_to_csv(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        filepath: Optional[str] = None,
    ) -> str:
        """Export candles to CSV file.

        Args:
            symbol: Trading pair symbol
            exchange: Exchange name
            timeframe: Candle timeframe
            filepath: Output file path (auto-generated if None)

        Returns:
            Path to the exported CSV file
        """
        candles = self.get_candles(symbol, exchange, timeframe)

        if not filepath:
            filepath = self.data_dir / f"{exchange}_{symbol}_{timeframe}.csv"

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for candle in candles:
                writer.writerow(
                    [
                        candle.timestamp.timestamp(),
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.volume,
                    ]
                )

        logger.info(f"Exported {len(candles)} candles to {filepath}")
        return str(filepath)

    def import_from_csv(self, filepath: str) -> int:
        """Import candles from CSV file.

        Args:
            filepath: Path to CSV file with columns: timestamp, open, high, low, close, volume

        Returns:
            Number of candles imported
        """
        from datetime import datetime

        candles = []
        with open(filepath, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append(
                    Candle(
                        symbol="UNKNOWN",
                        exchange="csv_import",
                        timeframe="1m",
                        timestamp=datetime.fromtimestamp(float(row["timestamp"])),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                )

        return self.save_candles(candles)

    def clear_data(
        self,
        symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> int:
        """Clear candle data from the database.

        Args:
            symbol: If provided, only clear this symbol
            exchange: If provided, only clear this exchange
            timeframe: If provided, only clear this timeframe

        Returns:
            Number of rows deleted
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        conditions = []
        params = []

        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if exchange:
            conditions.append("exchange = ?")
            params.append(exchange)
        if timeframe:
            conditions.append("timeframe = ?")
            params.append(timeframe)

        query = "DELETE FROM candles"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        cursor.execute(query, params)
        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        logger.info(f"Cleared {deleted} candles from {self.db_path}")
        return deleted


# Convenience functions
def get_datastore() -> DataStore:
    """Get or create a DataStore instance."""
    return DataStore()
