"""KuCoin exchange adapter with WebSocket market data.

Based on orbitr's websocket_market_service.py implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from ..core.models.market_data import MarketData

logger = logging.getLogger(__name__)


@dataclass
class KuCoinConfig:
    """KuCoin WebSocket configuration."""

    ws_url: str = "wss://ws-api.kucoin.com/endpoint"
    rest_url: str = "https://api.kucoin.com"
    ping_interval: int = 30
    ping_timeout: int = 10


class KuCoinAdapter:
    """KuCoin exchange adapter for market data and order execution.

    Features:
    - WebSocket real-time ticker data (best bid/ask)
    - REST fallback for order execution
    - Automatic reconnection on disconnect
    - Symbol normalization (BTCUSDT -> BTC-USDT)
    """

    def __init__(self, config: Optional[KuCoinConfig] = None):
        self.config = config or KuCoinConfig()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._price_cache: Dict[str, MarketData] = {}
        self._subscribers: list[Callable[[MarketData], None]] = []
        self._symbols: set[str] = set()

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to KuCoin format (BTC-USDT)."""
        s = symbol.upper().replace("-", "").replace("/", "")
        for quote in ["USDT", "USDC", "BTC", "ETH", "USD"]:
            if s.endswith(quote) and len(s) > len(quote):
                return f"{s[: -len(quote)]}-{quote}"
        return symbol.upper()

    def _denormalize_symbol(self, symbol: str) -> str:
        """Convert KuCoin format (BTC-USDT) to standard (BTCUSDT)."""
        return symbol.replace("-", "")

    async def _get_ws_url(self) -> str:
        """Get authenticated WebSocket URL with token."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.config.rest_url}/api/v1/bullet-public") as resp:
                    data = await resp.json()
                    if data.get("code") == "200000":
                        token = data["data"]["token"]
                        endpoint = data["data"]["instanceServers"][0]["endpoint"]
                        return f"{endpoint}?token={token}"
                    logger.error(f"KuCoin token error: {data}")
                    return self.config.ws_url
        except Exception as e:
            logger.error(f"Failed to get KuCoin token: {e}")
            return self.config.ws_url

    async def _handle_message(self, data: dict) -> None:
        """Handle incoming WebSocket message."""
        try:
            topic = data.get("topic", "")

            if data.get("type") == "message":
                if "ticker" in topic:
                    await self._handle_ticker_message(data)
                elif "candles" in topic:
                    await self._handle_candle_message(data)

        except Exception as e:
            logger.debug(f"Error handling KuCoin message: {e}")

    async def _handle_ticker_message(self, data: dict) -> None:
        """Handle ticker WebSocket message."""
        topic = data.get("topic", "")
        symbol = self._denormalize_symbol(topic.split(":")[-1])

        msg_data = data.get("data", {})
        if "bestBid" in msg_data and "bestAsk" in msg_data:
            best_bid = float(msg_data["bestBid"])
            best_ask = float(msg_data["bestAsk"])
            bid_size = float(msg_data.get("bestBidSize", 0))
            ask_size = float(msg_data.get("bestAskSize", 0))
            last_price = float(msg_data.get("last", best_bid))
            volume = float(msg_data.get("size", 0))

            market_data = MarketData(
                symbol=symbol,
                exchange="kucoin",
                bid=best_bid,
                ask=best_ask,
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

    async def _handle_candle_message(self, data: dict) -> None:
        """Handle candle WebSocket message.

        KuCoin candle data format:
        [timestamp, open, close, high, low, volume, turnover]
        """
        topic = data.get("topic", "")
        symbol = self._denormalize_symbol(topic.split(":")[-1].split("_")[0])

        msg_data = data.get("data", {})
        candle = msg_data.get("candles", [])

        if len(candle) >= 6:
            # Update price cache with latest candle close
            last_price = float(candle[2])
            market_data = MarketData(
                symbol=symbol,
                exchange="kucoin",
                bid=last_price,
                ask=last_price,
                bid_size=0,
                ask_size=0,
                last_price=last_price,
                volume=float(candle[5]),
                timestamp=datetime.utcnow(),
            )
            self._price_cache[symbol] = market_data

            for subscriber in self._subscribers:
                try:
                    subscriber(market_data)
                except Exception as e:
                    logger.warning(f"Subscriber error: {e}")

    async def _connection_handler(self) -> None:
        """Main WebSocket connection handler with auto-reconnect."""
        while self._running:
            try:
                url = await self._get_ws_url()
                logger.info(f"Connecting to KuCoin WebSocket: {len(self._symbols)} symbols")

                async with websockets.connect(
                    url,
                    ping_interval=self.config.ping_interval,
                    ping_timeout=self.config.ping_timeout,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info("✓ Connected to KuCoin WebSocket")

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
                logger.warning(f"KuCoin WebSocket closed: {e}")
            except Exception as e:
                logger.error(f"KuCoin WebSocket error: {type(e).__name__}: {e}")

            if self._running:
                logger.info(f"Reconnecting to KuCoin in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _subscribe(self) -> None:
        """Send subscription messages for all symbols."""
        if not self._ws:
            return

        formatted_symbols = [self._normalize_symbol(s) for s in self._symbols]

        for i, symbol in enumerate(formatted_symbols):
            subscribe_msg = {
                "id": str(int(time.time()) + i),
                "type": "subscribe",
                "topic": f"/market/ticker:{symbol}",
                "privateChannel": False,
                "response": True,
            }
            await self._ws.send(json.dumps(subscribe_msg))
            if i < 5:
                logger.info(f"Sent KuCoin subscribe: {symbol}")
            await asyncio.sleep(0.1)  # Rate limit protection

    async def _unsubscribe(self, symbols: list[str]) -> None:
        """Send unsubscribe messages."""
        if not self._ws:
            return

        for i, symbol in enumerate(symbols):
            normalized = self._normalize_symbol(symbol)
            unsubscribe_msg = {
                "id": str(int(time.time()) + i),
                "type": "unsubscribe",
                "topic": f"/market/ticker:{normalized}",
                "privateChannel": False,
                "response": True,
            }
            await self._ws.send(json.dumps(unsubscribe_msg))
            await asyncio.sleep(0.05)

    def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to symbol updates."""
        new_symbols = set(symbols) - self._symbols
        self._symbols.update(symbols)

        if self._running and new_symbols and self._ws:
            asyncio.create_task(self._subscribe_symbols(list(new_symbols)))

    async def _subscribe_symbols(self, symbols: list[str]) -> None:
        """Subscribe to new symbols (called after initial connection)."""
        if not self._ws:
            return

        for i, symbol in enumerate(symbols):
            normalized = self._normalize_symbol(symbol)
            subscribe_msg = {
                "id": str(int(time.time()) + i),
                "type": "subscribe",
                "topic": f"/market/ticker:{normalized}",
                "privateChannel": False,
                "response": True,
            }
            await self._ws.send(json.dumps(subscribe_msg))
            await asyncio.sleep(0.1)

    def subscribe_candles(self, symbols: list[str], timeframe: str = "1m") -> None:
        """Subscribe to candle updates for symbols.

        Args:
            symbols: Trading pair symbols
            timeframe: Candle timeframe ('1m', '5m', '15m', '1h', '4h', '1d')
        """
        normalized_symbols = [self._normalize_symbol(s) for s in symbols]
        for symbol in normalized_symbols:
            self._symbols.add(symbol)

        if self._running and self._ws:
            asyncio.create_task(self._subscribe_candles_symbols(list(normalized_symbols), timeframe))

    async def _subscribe_candles_symbols(self, symbols: list[str], timeframe: str) -> None:
        """Subscribe to candle data for symbols via WebSocket.

        KuCoin WebSocket candle topic: /market/candles:{symbol}_{type}
        where type is 1min, 5min, 15min, 1hour, 4hour, 1day
        """
        if not self._ws:
            return

        kucoin_timeframe = timeframe.replace("m", "min")

        for i, symbol in enumerate(symbols):
            subscribe_msg = {
                "id": str(int(time.time()) + i),
                "type": "subscribe",
                "topic": f"/market/candles:{symbol}_{kucoin_timeframe}",
                "privateChannel": False,
                "response": True,
            }
            await self._ws.send(json.dumps(subscribe_msg))
            if i < 5:
                logger.info(f"Sent KuCoin candle subscribe: {symbol} {kucoin_timeframe}")
            await asyncio.sleep(0.1)

    def add_subscriber(self, callback: Callable[[MarketData], None]) -> None:
        """Add a callback for market data updates."""
        self._subscribers.append(callback)

    def remove_subscriber(self, callback: Callable[[MarketData], None]) -> None:
        """Remove a market data subscriber."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def get_price(self, symbol: str) -> Optional[MarketData]:
        """Get cached price for symbol."""
        normalized = self._denormalize_symbol(symbol)
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
                url = f"{self.config.rest_url}/api/v1/market/orderbook/level1?symbol={normalized}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("code") == "200000":
                        return data["data"]
        except Exception as e:
            logger.error(f"Failed to fetch KuCoin ticker: {e}")
        return None

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", limit: int = 100
    ) -> Optional[list[dict]]:
        """Fetch OHLCV candles via REST API.

        Args:
            symbol: Trading pair symbol (e.g., 'BTCUSDT')
            timeframe: Candle timeframe ('1m', '5m', '15m', '1h', '4h', '1d')
            limit: Number of candles to fetch (max 2000)

        Returns:
            List of candle data or None
        """
        try:
            normalized = self._normalize_symbol(symbol)
            # KuCoin uses 1min, 5min, 15min, etc. (not 1m, 5m)
            kucoin_timeframe = timeframe.replace("m", "min")
            async with aiohttp.ClientSession() as session:
                url = f"{self.config.rest_url}/api/v1/market/candles?symbol={normalized}&type={kucoin_timeframe}&limit={limit}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("code") == "200000":
                        return data["data"]
                    else:
                        logger.warning(f"KuCoin OHLCV error: {data.get('msg')}")
        except Exception as e:
            logger.error(f"Failed to fetch KuCoin OHLCV: {e}")
        return None

    async def fetch_ohlcv_paginated(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 2000,
    ) -> list[dict]:
        """Fetch OHLCV candles using startAt for older historical data.

        Args:
            symbol: Trading pair symbol
            timeframe: Candle timeframe
            limit: Number of candles to fetch (max 2000 per request)

        Returns:
            List of all fetched candles sorted oldest-first
        """
        try:
            normalized = self._normalize_symbol(symbol)
            kucoin_timeframe = timeframe.replace("m", "min")

            # Use startAt to get older data (Unix timestamp in seconds)
            # Default to ~30 days back for 1m candles
            import time

            timeframe_seconds = {
                "1min": 60,
                "5min": 300,
                "15min": 900,
                "1hour": 3600,
                "4hour": 14400,
                "1day": 86400,
            }
            tf_seconds = timeframe_seconds.get(kucoin_timeframe, 60)

            back_seconds = tf_seconds * (limit + 500)
            start_at = int(time.time()) - back_seconds

            async with aiohttp.ClientSession() as session:
                url = f"{self.config.rest_url}/api/v1/market/candles?symbol={normalized}&type={kucoin_timeframe}&limit={limit}&startAt={start_at}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("code") != "200000":
                        logger.warning(f"KuCoin pagination error: {data.get('msg')}")
                        return []

                    candles = data["data"]
                    if not candles:
                        return []

                    # Sort oldest-first (timestamps are newest-first by default)
                    candles.sort(key=lambda x: int(x[0]))

                    logger.info(f"Fetched {len(candles)} candles from {normalized}")

                    return candles

        except Exception as e:
            logger.error(f"KuCoin pagination error: {e}")
            return []

    async def fetch_historical_by_period(
        self,
        symbol: str,
        timeframe: str = "1m",
        days: int = 30,
        candles_per_day: int = 1440,  # 1440 minutes in a day
    ) -> list[dict]:
        """Fetch historical candles for a specific period.

        Makes multiple requests to cover the requested number of days.

        Args:
            symbol: Trading pair symbol
            timeframe: Candle timeframe
            days: Number of days of historical data to fetch
            candles_per_day: Number of candles per day for the timeframe

        Returns:
            List of all fetched candles sorted oldest-first
        """
        import time

        normalized = self._normalize_symbol(symbol)
        kucoin_timeframe = timeframe.replace("m", "min")

        # Calculate timestamps - fetch from (now - days) to now
        now = int(time.time())
        start_at = now - (days * 86400)
        end_at = now

        all_candles = []
        seen: set[int] = set()

        try:
            async with aiohttp.ClientSession() as session:
                # Fetch up to 2000 candles (API limit)
                url = f"{self.config.rest_url}/api/v1/market/candles?symbol={normalized}&type={kucoin_timeframe}&limit=2000&startAt={start_at}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("code") != "200000":
                        logger.warning(f"KuCoin fetch error: {data.get('msg')}")
                        return []

                    candles = data.get("data", [])
                    if not candles:
                        return []

                    # Sort oldest-first
                    candles.sort(key=lambda x: int(x[0]))

                    # Filter to requested time range and deduplicate
                    for candle in candles:
                        ts = int(candle[0])
                        if start_at <= ts <= end_at and ts not in seen:
                            seen.add(ts)
                            all_candles.append(candle)

        except Exception as e:
            logger.error(f"KuCoin fetch error: {e}")
            return []

        logger.info(f"Fetched {len(all_candles)} candles for {symbol} over {days} days")
        return all_candles
