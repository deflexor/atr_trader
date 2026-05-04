"""Authenticated Bybit exchange client for order execution."""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable

import ccxt.pro as ccxtpro
import structlog

logger = structlog.get_logger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0


class ExchangeError(Exception):
    """Custom exception for exchange operations."""
    pass


def _normalize_symbol(symbol: str) -> str:
    """Convert raw symbol to ccxt format.

    "BTCUSDT" or "BTC/USDT" → "BTC/USDT"
    """
    clean = symbol.upper().strip()
    if "/" in clean:
        return clean
    return f"{clean.rstrip('USDT')}/USDT"


def _with_retry(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Retry decorator with exponential backoff on rate-limit errors."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        backoff = INITIAL_BACKOFF
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await fn(*args, **kwargs)
            except ccxtpro.RateLimitExceeded:
                if attempt == MAX_RETRIES:
                    raise
                logger.warning(
                    "rate_limit_hit",
                    function=fn.__name__,
                    attempt=attempt,
                    backoff=backoff,
                )
                await asyncio.sleep(backoff)
                backoff *= 2
        raise ExchangeError("unreachable")  # safety

    return wrapper


class ExchangeClient:
    """Authenticated Bybit client via ccxt.pro for order execution."""

    def __init__(
        self, api_key: str, api_secret: str, testnet: bool = False
    ) -> None:
        self._exchange = ccxtpro.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "sandbox": testnet,
                "enableRateLimit": True,
            }
        )
        self._log = logger.bind(component="exchange_client")

    # ── lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize exchange connection, load markets."""
        try:
            await self._exchange.load_markets()
            self._log.info("exchange_started", markets=len(self._exchange.markets))
        except ccxtpro.BaseError as exc:
            raise ExchangeError(f"start failed: {exc}") from exc

    async def stop(self) -> None:
        """Clean up exchange connection."""
        try:
            await self._exchange.close()
            self._log.info("exchange_stopped")
        except ccxtpro.BaseError as exc:
            self._log.warning("stop_error", error=str(exc))

    # ── order placement ────────────────────────────────────────

    @_with_retry
    async def place_limit_order(
        self, symbol: str, side: str, quantity: float, price: float
    ) -> dict:
        """Place a limit order. Returns {order_id, status, price, quantity}."""
        ccxt_symbol = _normalize_symbol(symbol)
        precise_price = self._exchange.price_to_precision(ccxt_symbol, price)
        precise_qty = self._exchange.amount_to_precision(ccxt_symbol, quantity)
        try:
            result = await self._exchange.create_order(
                ccxt_symbol, "limit", side, float(precise_qty), float(precise_price)
            )
            self._log.info(
                "limit_order_placed",
                symbol=ccxt_symbol,
                side=side,
                price=precise_price,
                quantity=precise_qty,
                order_id=result["id"],
            )
            return self._extract_order(result)
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"place_limit_order failed for {ccxt_symbol}: {exc}"
            ) from exc

    @_with_retry
    async def place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> dict:
        """Place a market order. Returns {order_id, status, avg_fill_price, quantity}."""
        ccxt_symbol = _normalize_symbol(symbol)
        precise_qty = self._exchange.amount_to_precision(ccxt_symbol, quantity)
        try:
            result = await self._exchange.create_order(
                ccxt_symbol, "market", side, float(precise_qty)
            )
            self._log.info(
                "market_order_placed",
                symbol=ccxt_symbol,
                side=side,
                quantity=precise_qty,
                order_id=result["id"],
            )
            return self._extract_order(result)
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"place_market_order failed for {ccxt_symbol}: {exc}"
            ) from exc

    # ── order management ───────────────────────────────────────

    @_with_retry
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order. Returns True on success (idempotent)."""
        ccxt_symbol = _normalize_symbol(symbol)
        try:
            order = await self._exchange.fetch_order(order_id, ccxt_symbol)
            if order["status"] in ("closed", "filled", "canceled"):
                self._log.info(
                    "cancel_skipped",
                    symbol=ccxt_symbol,
                    order_id=order_id,
                    status=order["status"],
                )
                return True
            await self._exchange.cancel_order(order_id, ccxt_symbol)
            self._log.info(
                "order_cancelled", symbol=ccxt_symbol, order_id=order_id
            )
            return True
        except ccxtpro.OrderNotFound:
            self._log.info(
                "cancel_idempotent", symbol=ccxt_symbol, order_id=order_id
            )
            return True
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"cancel_order failed for {ccxt_symbol}/{order_id}: {exc}"
            ) from exc

    # ── account data ───────────────────────────────────────────

    @_with_retry
    async def fetch_balance(self) -> dict:
        """Fetch account balance. Returns {total, free, used} per currency."""
        try:
            bal = await self._exchange.fetch_balance()
            return {
                currency: {
                    "total": info.get("total", 0.0),
                    "free": info.get("free", 0.0),
                    "used": info.get("used", 0.0),
                }
                for currency, info in bal.items()
                if isinstance(info, dict) and "total" in info
            }
        except ccxtpro.BaseError as exc:
            raise ExchangeError(f"fetch_balance failed: {exc}") from exc

    @_with_retry
    async def fetch_open_orders(self, symbol: str) -> list[dict]:
        """Fetch open orders for symbol. Returns [{id, side, price, quantity, status}]."""
        ccxt_symbol = _normalize_symbol(symbol)
        try:
            orders = await self._exchange.fetch_open_orders(ccxt_symbol)
            return [
                {
                    "id": o["id"],
                    "side": o["side"],
                    "price": o.get("price"),
                    "quantity": o.get("amount"),
                    "status": o["status"],
                }
                for o in orders
            ]
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"fetch_open_orders failed for {ccxt_symbol}: {exc}"
            ) from exc

    @_with_retry
    async def fetch_order_status(self, symbol: str, order_id: str) -> dict:
        """Fetch order status. Returns {status, filled, avg_fill_price}."""
        ccxt_symbol = _normalize_symbol(symbol)
        try:
            order = await self._exchange.fetch_order(order_id, ccxt_symbol)
            return {
                "status": order["status"],
                "filled": order.get("filled", 0.0),
                "avg_fill_price": order.get("average"),
            }
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"fetch_order_status failed for {ccxt_symbol}/{order_id}: {exc}"
            ) from exc

    # ── market data ────────────────────────────────────────────

    @_with_retry
    async def fetch_orderbook(self, symbol: str, limit: int = 10) -> dict:
        """Fetch order book. Returns {bids: [[price, qty]], asks: [[price, qty]]}."""
        ccxt_symbol = _normalize_symbol(symbol)
        try:
            book = await self._exchange.fetch_order_book(ccxt_symbol, limit)
            return {"bids": book["bids"], "asks": book["asks"]}
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"fetch_orderbook failed for {ccxt_symbol}: {exc}"
            ) from exc

    @_with_retry
    async def fetch_ticker(self, symbol: str) -> dict:
        """Fetch current ticker. Returns {bid, ask, last, high, low, volume}."""
        ccxt_symbol = _normalize_symbol(symbol)
        try:
            t = await self._exchange.fetch_ticker(ccxt_symbol)
            return {
                "bid": t.get("bid"),
                "ask": t.get("ask"),
                "last": t.get("last"),
                "high": t.get("high"),
                "low": t.get("low"),
                "volume": t.get("baseVolume"),
            }
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"fetch_ticker failed for {ccxt_symbol}: {exc}"
            ) from exc

    # ── helpers ────────────────────────────────────────────────

    @staticmethod
    def _extract_order(result: dict) -> dict:
        """Normalize ccxt order response to a consistent dict."""
        return {
            "order_id": result["id"],
            "status": result["status"],
            "price": result.get("price"),
            "quantity": result.get("amount"),
            "avg_fill_price": result.get("average"),
        }
