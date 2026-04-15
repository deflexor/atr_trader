"""Bybit exchange adapter with WebSocket market data.

Based on orbitr's websocket_market_service.py implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Optional, Callable, Any
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from ..core.models.market_data import MarketData

logger = logging.getLogger(__name__)


@dataclass
class BybitConfig:
    """Bybit WebSocket configuration."""

    ws_url: str = "wss://stream.bybit.com/v5/public/spot"
    rest_url: str = "https://api.bybit.com"
    ping_interval: int = 20


class BybitAdapter:
    """Bybit exchange adapter for market data and order execution.

    Features:
    - WebSocket real-time ticker data (best bid/ask)
    - REST fallback for order execution
    - Automatic reconnection on disconnect
    - Symbol format: BTCUSDT (no separator)
    """

    def __init__(self, config: Optional[BybitConfig] = None):
        self.config = config or BybitConfig()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._price_cache: Dict[str, MarketData] = {}
        self._subscribers: list[Callable[[MarketData], None]] = []
        self._symbols: set[str] = set()

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to Bybit format (BTCUSDT)."""
        return symbol.upper().replace("-", "").replace("/", "")

    async def _handle_message(self, data: dict) -> None:
        """Handle incoming WebSocket message."""
        try:
            if data.get("topic", "").startswith("tickers."):
                topic = data.get("topic", "")
                symbol = topic.replace("tickers.", "")

                msg_data = data.get("data", {})
                if "lastPrice" in msg_data:
                    last_price = float(msg_data["lastPrice"])
                    bid_price = float(msg_data.get("bidPrice", last_price * 0.9999))
                    ask_price = float(msg_data.get("askPrice", last_price * 1.0001))
                    bid_size = float(msg_data.get("bidSize", 0))
                    ask_size = float(msg_data.get("askSize", 0))
                    volume = float(msg_data.get("volume24h", 0))

                    market_data = MarketData(
                        symbol=symbol,
                        exchange="bybit",
                        bid=bid_price,
                        ask=ask_price,
                        bid_size=bid_size,
                        ask_size=ask_size,
                        last_price=last_price,
                        volume=volume,
                        timestamp=datetime.utcnow(),
                    )

                    self._price_cache[symbol] = market_data

                    for subscriber in self._subscribers:
                        try:
                            subscriber(market_data)
                        except Exception as e:
                            logger.warning(f"Subscriber error: {e}")

        except Exception as e:
            logger.debug(f"Error handling Bybit message: {e}")

    async def _connection_handler(self) -> None:
        """Main WebSocket connection handler with auto-reconnect."""
        while self._running:
            try:
                logger.info(f"Connecting to Bybit WebSocket: {len(self._symbols)} symbols")

                async with websockets.connect(
                    self.config.ws_url, ping_interval=self.config.ping_interval
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info("✓ Connected to Bybit WebSocket")

                    # Subscribe to symbols
                    await self._subscribe()

                    async for message in ws:
                        if not self._running:
                            break

                        try:
                            data = json.loads(message)
                            await self._handle_message(data)
                        except json.JSONDecodeError:
                            logger.debug(f"Invalid JSON: {str(message)[:100]}")

            except ConnectionClosed as e:
                logger.warning(f"Bybit WebSocket closed: {e}")
            except Exception as e:
                logger.error(f"Bybit WebSocket error: {type(e).__name__}: {e}")

            if self._running:
                logger.info(f"Reconnecting to Bybit in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _subscribe(self) -> None:
        """Send subscription messages for all symbols (max 10 per message)."""
        if not self._ws:
            return

        args = [f"tickers.{self._normalize_symbol(s)}" for s in self._symbols]
        batch_size = 10

        for i in range(0, len(args), batch_size):
            batch = args[i : i + batch_size]
            subscribe_msg = {"op": "subscribe", "args": batch}
            await self._ws.send(json.dumps(subscribe_msg))
            logger.info(f"Sent Bybit subscribe batch {i // batch_size + 1}")
            await asyncio.sleep(0.5)  # Rate limit protection

    async def _subscribe_symbols(self, symbols: list[str]) -> None:
        """Subscribe to new symbols after connection is established."""
        if not self._ws:
            return

        args = [f"tickers.{self._normalize_symbol(s)}" for s in symbols]
        batch_size = 10

        for i in range(0, len(args), batch_size):
            batch = args[i : i + batch_size]
            subscribe_msg = {"op": "subscribe", "args": batch}
            await self._ws.send(json.dumps(subscribe_msg))
            await asyncio.sleep(0.3)

    def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to symbol updates."""
        new_symbols = set(symbols) - self._symbols
        self._symbols.update(symbols)

        if self._running and new_symbols and self._ws:
            asyncio.create_task(self._subscribe_symbols(list(new_symbols)))

    def add_subscriber(self, callback: Callable[[MarketData], None]) -> None:
        """Add a callback for market data updates."""
        self._subscribers.append(callback)

    def remove_subscriber(self, callback: Callable[[MarketData], None]) -> None:
        """Remove a market data subscriber."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def get_price(self, symbol: str) -> Optional[MarketData]:
        """Get cached price for symbol."""
        normalized = self._normalize_symbol(symbol)
        return self._price_cache.get(normalized)

    async def start(self) -> None:
        """Start the WebSocket connection."""
        self._running = True
        asyncio.create_task(self._connection_handler())

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
        self._ws = None

    async def fetch_ticker(self, symbol: str) -> Optional[dict]:
        """Fetch ticker via REST API (fallback)."""
        try:
            normalized = self._normalize_symbol(symbol)
            async with aiohttp.ClientSession() as session:
                url = f"{self.config.rest_url}/v5/market/tickers?category=spot&symbol={normalized}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("retCode") == 0:
                        return data["result"]["list"][0] if data["result"]["list"] else None
        except Exception as e:
            logger.error(f"Failed to fetch Bybit ticker: {e}")
        return None

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1", limit: int = 100
    ) -> Optional[list[dict]]:
        """Fetch OHLCV candles via REST API."""
        try:
            normalized = self._normalize_symbol(symbol)
            async with aiohttp.ClientSession() as session:
                url = f"{self.config.rest_url}/v5/market/kline?category=spot&symbol={normalized}&interval={timeframe}&limit={limit}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("retCode") == 0:
                        return data["result"]["list"]
        except Exception as e:
            logger.error(f"Failed to fetch Bybit OHLCV: {e}")
        return None

    async def fetch_ohlcv_paginated(
        self,
        symbol: str,
        timeframe: str = "1",
        limit: int = 1000,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> list[dict]:
        """Fetch OHLCV candles with pagination for historical data.

        Args:
            symbol: Trading pair symbol (e.g., 'BTCUSDT')
            timeframe: Candle timeframe (1, 5, 15, 30, 60, 240, 1000, 2000)
            limit: Number of candles per request (max 1000)
            start_time: Start timestamp in milliseconds (optional) - stop pagination at this point
            end_time: End timestamp in milliseconds (optional)

        Returns:
            List of all fetched candles sorted oldest-first
        """
        all_candles = []
        current_end = end_time

        try:
            async with aiohttp.ClientSession() as session:
                page_count = 0
                max_pages = 200  # Safety limit - increased for longer periods

                while page_count < max_pages:
                    page_count += 1
                    params = {
                        "category": "spot",
                        "symbol": self._normalize_symbol(symbol),
                        "interval": timeframe,
                        "limit": min(limit, 1000),
                    }

                    if current_end:
                        params["end"] = current_end

                    url = f"{self.config.rest_url}/v5/market/kline"
                    async with session.get(url, params=params) as resp:
                        data = await resp.json()
                        if data.get("retCode") != 0:
                            logger.warning(f"Bybit pagination error: {data.get('retMsg')}")
                            break

                        candles = data["result"]["list"]
                        if not candles:
                            break

                        all_candles.extend(candles)

                        # Set end to oldest timestamp - 1ms to get previous batch
                        oldest_ts = int(candles[-1][0])
                        current_end = oldest_ts - 1

                        # Small delay to avoid rate limits
                        await asyncio.sleep(0.1)

                        # If we got fewer than limit, we're at the end
                        if len(candles) < 1000:
                            break

                        # Stop if we've reached the start_time limit
                        if start_time and oldest_ts <= start_time:
                            logger.info(f"Reached start_time limit, stopping pagination")
                            break

                # Sort oldest-first and deduplicate
                all_candles.sort(key=lambda x: int(x[0]))
                seen = set()
                result = []
                for candle in all_candles:
                    ts = int(candle[0])
                    if ts not in seen:
                        seen.add(ts)
                        result.append(candle)
                all_candles = result

                logger.info(f"Bybit fetched {len(all_candles)} candles for {symbol}")

        except Exception as e:
            logger.error(f"Bybit pagination error: {e}")

        return all_candles

    async def fetch_historical_by_period(
        self,
        symbol: str,
        timeframe: str = "1",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> list[dict]:
        """Fetch historical candles for a specific date range.

        Args:
            symbol: Trading pair symbol
            timeframe: Candle timeframe
            start_date: Start datetime (defaults to ~60 days ago)
            end_date: End datetime (defaults to now)

        Returns:
            List of all fetched candles sorted oldest-first
        """
        import time

        end_time = int((end_date or datetime.now()).timestamp() * 1000)
        start_time = int((start_date or datetime.now() - timedelta(days=60)).timestamp() * 1000)

        # Map timeframe string to Bybit interval
        interval_map = {
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "1h": "60",
            "4h": "240",
            "1d": "D",
            "1w": "W",
        }
        interval = interval_map.get(timeframe, timeframe)

        return await self.fetch_ohlcv_paginated(
            symbol=symbol,
            timeframe=interval,
            start_time=start_time,
            end_time=end_time,
        )

    async def fetch_orderbook(self, symbol: str, limit: int = 50) -> Optional[dict]:
        """Fetch order book via REST API."""
        try:
            normalized = self._normalize_symbol(symbol)
            async with aiohttp.ClientSession() as session:
                url = f"{self.config.rest_url}/v5/market/orderbook?category=spot&symbol={normalized}&limit={limit}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("retCode") == 0:
                        return data["result"]
        except Exception as e:
            logger.error(f"Failed to fetch Bybit orderbook: {e}")
        return None
