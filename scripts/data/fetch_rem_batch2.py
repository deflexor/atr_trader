"""Fetch remaining 6 symbols in smaller batches to avoid timeouts."""

import asyncio
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import ccxt.async_support as ccxt

BINANCE = ["BCH/USDT", "SUI/USDT", "SHIB/USDT", "XAUT/USDT", "PAXG/USDT"]
BYBIT = ["MNT/USDT:USDT"]
TIMEFRAME = "5m"
DAYS = 800


def db_symbol(s):
    return s.split("/")[0] + "USDT"


async def fetch_batch(exchange, symbol, since_ms, until_ms):
    all_c = []
    cur = since_ms
    retries = 0
    while cur < until_ms:
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, since=cur, limit=1000)
            retries = 0
        except Exception as e:
            retries += 1
            if retries > 5:
                print(f"  Too many errors for {symbol}: {e}")
                break
            print(f"  Err {symbol}: {e}")
            await asyncio.sleep(3)
            continue
        if not ohlcv:
            break
        all_c.extend(ohlcv)
        last = ohlcv[-1][0]
        if last <= cur:
            break
        cur = last + 1
        await asyncio.sleep(0.12)
        if len(all_c) % 40000 == 0:
            print(f"  {symbol}: {len(all_c):,}")
    return all_c


def save(rows, sym, ex):
    conn = sqlite3.connect("data/candles.db")
    c = conn.cursor()
    vals = [(sym, ex, TIMEFRAME, int(r[0]/1000), r[1], r[2], r[3], r[4], r[5] or 0) for r in rows]
    c.execute("BEGIN IMMEDIATE")
    try:
        c.executemany(
            "INSERT OR IGNORE INTO candles(symbol,exchange,timeframe,timestamp,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?,?)",
            vals)
        conn.commit()
        ins = c.rowcount
    except Exception as e:
        conn.rollback()
        print(f"  DB err: {e}")
        ins = 0
    conn.close()
    return ins


async def main():
    until = int(datetime.now(timezone.utc).timestamp() * 1000)
    since = until - DAYS * 86400 * 1000
    print(f"Fetching up to {DAYS} days")

    # Binance
    if BINANCE:
        ex = ccxt.binance({"enableRateLimit": True})
        try:
            for s in BINANCE:
                db = db_symbol(s)
                print(f"\n{s} ({db})")
                candles = await fetch_batch(ex, s, since, until)
                print(f"  Got {len(candles):,}")
                if candles:
                    d0 = datetime.fromtimestamp(candles[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    d1 = datetime.fromtimestamp(candles[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    print(f"  {d0} → {d1}")
                    ins = save(candles, db, "binance")
                    print(f"  Saved {ins:,}")
        finally:
            await ex.close()

    # Bybit
    if BYBIT:
        ex = ccxt.bybit({"enableRateLimit": True})
        try:
            for s in BYBIT:
                db = db_symbol(s)
                print(f"\n{s} ({db})")
                candles = await fetch_batch(ex, s, since, until)
                print(f"  Got {len(candles):,}")
                if candles:
                    d0 = datetime.fromtimestamp(candles[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    d1 = datetime.fromtimestamp(candles[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    print(f"  {d0} → {d1}")
                    ins = save(candles, db, "bybit")
                    print(f"  Saved {ins:,}")
        finally:
            await ex.close()

    print("\n=== Done ===")
    conn = sqlite3.connect("data/candles.db")
    c = conn.cursor()
    c.execute("SELECT symbol, exchange, COUNT(*) FROM candles WHERE timeframe='5m' GROUP BY symbol, exchange ORDER BY symbol")
    for r in c.fetchall():
        print(f"  {r[0]:12s} {r[1]:8s} {r[2]:>9,}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
