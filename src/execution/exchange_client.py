"""Authenticated Bybit exchange client for order execution (USDT perpetuals)."""
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


def _normalize_symbol(symbol: str, market_type: str = "perp") -> str:
    """Convert raw symbol to ccxt unified format.

    market_type="perp" (default):
        "BTCUSDT" or "BTC/USDT" → "BTC/USDT:USDT"
    market_type="spot":
        "BTCUSDT" or "BTC/USDT" → "BTC/USDT"
    """
    clean = symbol.upper().strip()
    if ":" in clean:
        return clean  # Already fully qualified
    base = clean.split("/")[0] if "/" in clean else clean.rstrip("USDT")
    if market_type == "perp":
        return f"{base}/USDT:USDT"
    return f"{base}/USDT"


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
    """Authenticated Bybit client via ccxt.pro for USDT perpetual trading."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        market_type: str = "perp",
        leverage: int = 1,
    ) -> None:
        self._exchange = ccxtpro.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "sandbox": testnet,
                "enableRateLimit": True,
            }
        )
        self._market_type = market_type
        self._leverage = leverage
        self._log = logger.bind(component="exchange_client")

    # ── lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize exchange connection, load markets."""
        try:
            await self._exchange.load_markets()
            self._log.info(
                "exchange_started",
                markets=len(self._exchange.markets),
                market_type=self._market_type,
                leverage=self._leverage,
            )
        except ccxtpro.BaseError as exc:
            raise ExchangeError(f"start failed: {exc}") from exc

    async def setup_perp_symbol(self, symbol: str) -> None:
        """Configure a symbol for USDT perpetual trading.

        Order matters: position_mode → margin_mode → leverage.
        Idempotent — safe to call on every startup.
        """
        if self._market_type != "perp":
            return

        ccxt_symbol = _normalize_symbol(symbol, "perp")
        lev = str(self._leverage)
        log = self._log.bind(symbol=ccxt_symbol, leverage=lev)

        try:
            # 1. One-way position mode (not hedge)
            await self._exchange.set_position_mode(False, ccxt_symbol)
            log.debug("position_mode_set", mode="one_way")
        except ccxtpro.BaseError as exc:
            if "not modified" not in str(exc).lower():
                log.warning("position_mode_failed", error=str(exc))

        try:
            # 2. Isolated margin (Bybit requires leverage param here)
            await self._exchange.set_margin_mode(
                "isolated", ccxt_symbol, {"leverage": lev}
            )
            log.debug("margin_mode_set", mode="isolated")
        except ccxtpro.BaseError as exc:
            # "not modified" is fine — already in isolated mode
            if "not modified" not in str(exc).lower():
                log.warning("margin_mode_failed", error=str(exc))

        try:
            # 3. Set leverage
            await self._exchange.set_leverage(self._leverage, ccxt_symbol)
            log.info("perp_configured", leverage=self._leverage)
        except ccxtpro.BaseError as exc:
            if "not modified" not in str(exc).lower():
                log.warning("leverage_set_failed", error=str(exc))

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
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        reduce_only: bool = False,
    ) -> dict:
        """Place a limit order. Returns {order_id, status, price, quantity}."""
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
        precise_price = self._exchange.price_to_precision(ccxt_symbol, price)
        precise_qty = self._exchange.amount_to_precision(ccxt_symbol, quantity)
        if float(precise_qty) <= 0:
            raise ExchangeError(
                f"quantity {quantity} rounds to {precise_qty} for {ccxt_symbol}"
            )
        params = {"reduceOnly": reduce_only} if reduce_only else {}
        try:
            result = await self._exchange.create_order(
                ccxt_symbol,
                "limit",
                side,
                float(precise_qty),
                float(precise_price),
                params,
            )
            self._log.info(
                "limit_order_placed",
                symbol=ccxt_symbol,
                side=side,
                price=precise_price,
                quantity=precise_qty,
                order_id=result["id"],
                reduce_only=reduce_only,
            )
            return self._extract_order(result)
        except ccxtpro.BaseError as exc:
            raise ExchangeError(
                f"place_limit_order failed for {ccxt_symbol}: {exc}"
            ) from exc

    @_with_retry
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
    ) -> dict:
        """Place a market order. Returns {order_id, status, avg_fill_price, quantity}."""
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
        precise_qty = self._exchange.amount_to_precision(ccxt_symbol, quantity)
        if float(precise_qty) <= 0:
            raise ExchangeError(
                f"quantity {quantity} rounds to {precise_qty} for {ccxt_symbol}"
            )
        params = {"reduceOnly": reduce_only} if reduce_only else {}
        try:
            result = await self._exchange.create_order(
                ccxt_symbol, "market", side, float(precise_qty), None, params
            )
            self._log.info(
                "market_order_placed",
                symbol=ccxt_symbol,
                side=side,
                quantity=precise_qty,
                order_id=result["id"],
                reduce_only=reduce_only,
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
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
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
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
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
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
        try:
            order = await self._exchange.fetch_order(
                order_id, ccxt_symbol, {"acknowledged": True}
            )
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
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
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
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
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

    @_with_retry
    async def fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch current funding rate for a perpetual symbol.

        Returns {rate, next_rate, next_time} or empty dict on error.
        """
        ccxt_symbol = _normalize_symbol(symbol, self._market_type)
        try:
            fr = await self._exchange.fetch_funding_rate(ccxt_symbol)
            return {
                "rate": fr.get("fundingRate"),
                "next_rate": fr.get("nextFundingRate"),
                "next_time": fr.get("nextFundingDatetime"),
            }
        except ccxtpro.BaseError as exc:
            self._log.warning(
                "fetch_funding_rate_failed", symbol=ccxt_symbol, error=str(exc)
            )
            return {}

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
