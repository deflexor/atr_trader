"""Fetch 5m candles for expansion symbol set: 11 new assets for cointegration screening.

New symbols requested: BNB, TON, COMP ("CC"), XLM, BCH, SUI, SHIB, MNT, XAUT, PAXG

Strategy: Binance for most (longer history), Bybit for Bybit-only tokens.

Usage:
    uv run python scripts/data/fetch_expansion_symbols.py
"""

import asyncio
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

import ccxt.async_support as ccxt

# Binance spot: most symbols
BINANCE_SYMBOLS = [
    "BNB/USDT", "TON/USDT", "COMP/USDT", "XLM/USDT",
    "BCH/USDT", "SUI/USDT", "SHIB/USDT",
    "XAUT/USDT", "PAXG/USDT",
]
# Bybit perps: MNT (Mantle not on Binance spot)
BYBIT_SYMBOLS = ["MNT/USDT:USDT"]

TIMEFRAME = "5m"
DAYS = 800
BATCH_SIZE = 1000


def db_symbol(ccxt_sym: str) -> str:
    return ccxt_sym.split("/")[0] + "USDT"


async def fetch_candles(exchange, symbol: str, since_ms: int, until_ms: int):
    all_candles = []
    current = since_ms
    retries = 0

    while current < until_ms:
        try:
            ohlcv = await exchange.fetch_ohlcv(
                symbol, TIMEFRAME, since=current, limit=BATCH_SIZE
            )
            retries = 0
        except Exception as e:
            retries += 1
            if retries > 5:
                print(f"  Too many errors for {symbol}, stopping. Last: {e}")
                break
            print(f"  Error fetching {symbol} at {current}: {e}")
            await asyncio.sleep(2)
            continue

        if not ohlcv:
            break

        all_candles.extend(ohlcv)
        last_ts = ohlcv[-1][0]
        if last_ts <= current:
            break
        current = last_ts + 1

        await asyncio.sleep(0.15)

        if len(all_candles) % 20000 == 0:
            pct = (current - since_ms) / (until_ms - since_ms) * 100
            print(f"  {symbol}: {len(all_candles):,} candles ({min(pct, 100):.0f}%)...")

    return all_candles


def save_to_db(candles_raw: list, sym: str, exchange_name: str) -> int:
    conn = sqlite3.connect("data/candles.db")
    cursor = conn.cursor()

    rows = []
    for c in candles_raw:
        ts_ms, o, h, l, cl, vol = c[:6]
        ts_sec = int(ts_ms / 1000)
        rows.append((sym, exchange_name, TIMEFRAME, ts_sec, o, h, l, cl, vol or 0.0))

    cursor.execute("BEGIN IMMEDIATE")
    try:
        cursor.executemany(
            """INSERT OR IGNORE INTO candles
               (symbol, exchange, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        inserted = cursor.rowcount
    except Exception as e:
        conn.rollback()
        print(f"  DB error: {e}")
        inserted = 0
    finally:
        conn.close()

    return inserted


async def main():
    until_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = until_ms - DAYS * 86400 * 1000

    print(f"Fetching {DAYS} days of 5m candles for {len(BINANCE_SYMBOLS) + len(BYBIT_SYMBOLS)} symbols")
    print(f"Since: {datetime.fromtimestamp(since_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print()

    # --- Binance symbols ---
    if BINANCE_SYMBOLS:
        print("=== Binance symbols ===")
        exchange = ccxt.binance({"enableRateLimit": True})
        try:
            for ccxt_sym in BINANCE_SYMBOLS:
                sym = db_symbol(ccxt_sym)
                print(f"\nFetching {ccxt_sym} (DB: {sym})...")
                candles = await fetch_candles(exchange, ccxt_sym, since_ms, until_ms)
                print(f"  Got {len(candles):,} candles")

                if candles:
                    d0 = datetime.fromtimestamp(candles[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    d1 = datetime.fromtimestamp(candles[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    print(f"  Range: {d0} → {d1}")
                    inserted = save_to_db(candles, sym, "binance")
                    print(f"  Saved {inserted:,} new candles")
                else:
                    print(f"  No data")
        finally:
            await exchange.close()

    # --- Bybit symbols ---
    if BYBIT_SYMBOLS:
        print("\n=== Bybit symbols ===")
        exchange = ccxt.bybit({"enableRateLimit": True})
        try:
            for ccxt_sym in BYBIT_SYMBOLS:
                sym = db_symbol(ccxt_sym)
                print(f"\nFetching {ccxt_sym} (DB: {sym})...")
                candles = await fetch_candles(exchange, ccxt_sym, since_ms, until_ms)
                print(f"  Got {len(candles):,} candles")

                if candles:
                    d0 = datetime.fromtimestamp(candles[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    d1 = datetime.fromtimestamp(candles[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    print(f"  Range: {d0} → {d1}")
                    inserted = save_to_db(candles, sym, "bybit")
                    print(f"  Saved {inserted:,} new candles")
                else:
                    print(f"  No data")
        finally:
            await exchange.close()

    # --- Summary ---
    print("\n=== Full Database Summary ===")
    conn = sqlite3.connect("data/candles.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT symbol, exchange, COUNT(*), MIN(timestamp), MAX(timestamp)
        FROM candles WHERE timeframe='5m'
        GROUP BY symbol, exchange ORDER BY symbol
    """)
    for r in cursor.fetchall():
        days = (r[4] - r[3]) / 86400
        d0 = datetime.fromtimestamp(r[3], tz=timezone.utc).strftime("%Y-%m-%d")
        d1 = datetime.fromtimestamp(r[4], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  {r[0]:12s} {r[1]:8s} {r[2]:>9,} candles  {days:>6.0f}d  {d0} → {d1}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
