"""Fetch 5m candles for additional symbols from Bybit via ccxt.

Downloads SOLUSDT, ADAUSDT, AVAXUSDT, XMRUSDT, UNIUSDT 5m candles
for the same date range as existing data in candles.db.
"""

import asyncio
import sys
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt

SYMBOLS = ["SOL/USDT", "ADA/USDT", "AVAX/USDT", "XMR/USDT", "UNI/USDT"]
TIMEFRAME = "5m"
BATCH_SIZE = 1000  # candles per request


async def fetch_symbol(exchange, symbol: str, since_ms: int, until_ms: int):
    """Fetch all 5m candles for a symbol in batches."""
    all_candles = []
    current = since_ms

    while current < until_ms:
        try:
            ohlcv = await exchange.fetch_ohlcv(
                symbol, TIMEFRAME, since=current, limit=BATCH_SIZE
            )
        except Exception as e:
            print(f"  Error fetching {symbol} at {current}: {e}")
            await asyncio.sleep(1)
            continue

        if not ohlcv:
            break

        all_candles.extend(ohlcv)
        last_ts = ohlcv[-1][0]
        if last_ts <= current:
            break
        current = last_ts + 1  # Move past last fetched

        # Rate limit: Bybit allows ~10 req/s
        await asyncio.sleep(0.15)

        if len(all_candles) % 5000 == 0:
            print(f"  {symbol}: {len(all_candles)} candles fetched...")

    return all_candles


async def main():
    # Determine date range from existing data
    import sqlite3
    conn = sqlite3.connect("data/candles.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM candles WHERE timeframe='5m' AND symbol='ETHUSDT'"
    )
    row = cursor.fetchone()
    conn.close()

    if row and row[0] and row[1]:
        since_ms = row[0] * 1000 if row[0] < 1e12 else row[0]
        until_ms = row[1] * 1000 if row[1] < 1e12 else row[1]
        print(f"Date range: {datetime.fromtimestamp(since_ms/1000, tz=timezone.utc)} to {datetime.fromtimestamp(until_ms/1000, tz=timezone.utc)}")
    else:
        # Default: last 180 days
        until_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        since_ms = until_ms - 180 * 86400 * 1000
        print(f"Using default 180-day range")

    exchange = ccxt.bybit({"enableRateLimit": True})

    try:
        for symbol in SYMBOLS:
            ccxt_symbol = symbol  # e.g., "SOL/USDT"
            db_symbol = symbol.replace("/", "") + "DT" if not symbol.endswith("USDT") else symbol.replace("/", "")

            # Normalize: SOL/USDT -> SOLUSDT
            db_symbol = symbol.replace("/", "").replace("USDT", "USDT")
            # Actually: SOL/USDT -> SOLUSDT
            db_symbol = symbol.replace("/", "")

            print(f"Fetching {ccxt_symbol} (DB: {db_symbol})...")
            candles = await fetch_symbol(exchange, ccxt_symbol, since_ms, until_ms)
            print(f"  Got {len(candles)} candles")

            # Save to database
            from src.core.models.candle import Candle
            from src.core.db.datastore import DataStore

            ds = DataStore()
            candle_objects = []
            for c in candles:
                ts_ms, o, h, l, cl, vol = c[:6]
                ts_sec = int(ts_ms / 1000)
                candle_obj = Candle(
                    symbol=db_symbol,
                    exchange="bybit",
                    timeframe="5m",
                    timestamp=datetime.fromtimestamp(ts_sec, tz=timezone.utc),
                    open=o,
                    high=h,
                    low=l,
                    close=cl,
                    volume=vol or 0.0,
                )
                candle_objects.append(candle_obj)

            if candle_objects:
                saved = ds.save_candles(candle_objects)
                print(f"  Saved {saved} candles to DB")
            else:
                print(f"  No candles to save")

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
