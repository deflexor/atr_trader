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
            if data.get("type") == "message" and "ticker" in data.get("topic", ""):
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

        except Exception as e:
            logger.debug(f"Error handling KuCoin message: {e}")

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
        """Fetch OHLCV candles via REST API."""
        try:
            normalized = self._normalize_symbol(symbol)
            async with aiohttp.ClientSession() as session:
                url = f"{self.config.rest_url}/api/v1/market/candles?symbol={normalized}&type={timeframe}&limit={limit}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get("code") == "200000":
                        return data["data"]
        except Exception as e:
            logger.error(f"Failed to fetch KuCoin OHLCV: {e}")
        return None
