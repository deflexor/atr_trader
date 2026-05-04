"""Order lifecycle management with limit-order priority and slippage measurement."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import structlog

from .exchange_client import ExchangeError

logger = structlog.get_logger(__name__)

POLL_INTERVAL_SECONDS = 2


@dataclass(frozen=True)
class OrderResult:
    """Immutable result of an order operation."""

    order_id: str
    symbol: str
    side: str
    quantity: float
    filled_quantity: float
    fill_price: Optional[float]
    slippage_pct: Optional[float]
    status: str  # filled, partial, rejected, cancelled, error
    commission: float = 0.0
    exchange_order_id: Optional[str] = None
    context: Optional[dict] = None


def _build_order_dict(
    order_id: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    reason: str,
    position_id: Optional[str],
    price: Optional[float] = None,
    signal_price: Optional[float] = None,
    context: Optional[dict] = None,
) -> dict:
    """Build a dict suitable for StateManager.save_order()."""
    return {
        "id": order_id,
        "position_id": position_id,
        "symbol": symbol,
        "exchange": "bybit",
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "price": price,
        "status": "pending",
        "filled_quantity": 0.0,
        "avg_fill_price": None,
        "signal_price": signal_price,
        "slippage_pct": None,
        "commission": 0.0,
        "reason": reason,
        "context": context,
    }


def _calc_limit_price(mid_price: float, side: str) -> float:
    """Calculate limit price with 2bp offset from mid.

    Buys: slightly below mid (maker bids under mid).
    Sells: slightly above mid (maker asks over mid).
    """
    offset = mid_price * 0.0002  # 2 basis points
    if side == "buy":
        return mid_price - offset
    return mid_price + offset


def _map_ccxt_status(raw_status: str) -> str:
    """Map ccxt order status to our normalised status."""
    mapping = {
        "closed": "filled",
        "filled": "filled",
        "open": "partial",
        "partially_filled": "partial",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "rejected": "rejected",
        "expired": "cancelled",
    }
    return mapping.get(raw_status, "error")


def _calc_commission(
    filled_quantity: float, fill_price: Optional[float], commission_pct: float
) -> float:
    """Calculate commission on a fill."""
    if fill_price is None or filled_quantity <= 0:
        return 0.0
    return filled_quantity * fill_price * commission_pct


class OrderManager:
    """Order placement with limit-order priority and slippage measurement."""

    def __init__(
        self,
        exchange_client,
        slippage_guard,
        state_manager,
        limit_order_wait_seconds: int = 30,
        commission_pct: float = 0.0006,
    ) -> None:
        self._client = exchange_client
        self._guard = slippage_guard
        self._state = state_manager
        self._limit_wait = limit_order_wait_seconds
        self._commission_pct = commission_pct
        self._log = logger.bind(component="order_manager")

    # ── public API ───────────────────────────────────────────────

    async def place_entry_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        signal_price: float,
        reason: str = "signal",
        position_id: Optional[str] = None,
        signal_context: Optional[dict] = None,
    ) -> OrderResult:
        """Place an entry order with limit-order priority.

        Flow: pre-trade check → limit order → poll → market fallback.
        Never raises; returns OrderResult(status='error') on exceptions.

        signal_context: dict with signal/market data for debugging.
            e.g. {"signal": {...}, "market": {...}}
        """
        order_id = _gen_id()
        order_dict = _build_order_dict(
            order_id, symbol, side, "limit", quantity,
            reason, position_id, signal_price=signal_price,
            context=signal_context,
        )
        try:
            return await self._execute_entry(
                order_id, order_dict, symbol, side, quantity, signal_price,
            )
        except ExchangeError as exc:
            self._log.warning("entry_error", symbol=symbol, error=str(exc))
            return await self._finalize_error(order_dict, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("entry_unexpected_error", symbol=symbol, error=str(exc))
            return await self._finalize_error(order_dict, str(exc))

    async def place_exit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reason: str = "trailing_stop",
        position_id: Optional[str] = None,
    ) -> OrderResult:
        """Place an exit order — always market for speed on stop-losses."""
        order_id = _gen_id()
        order_dict = _build_order_dict(
            order_id, symbol, side, "market", quantity,
            reason, position_id,
        )
        try:
            return await self._execute_market_order(order_dict)
        except ExchangeError as exc:
            self._log.error("exit_error", symbol=symbol, error=str(exc))
            return await self._finalize_error(order_dict, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._log.error("exit_unexpected_error", symbol=symbol, error=str(exc))
            return await self._finalize_error(order_dict, str(exc))

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        try:
            return await self._client.cancel_order(symbol, order_id)
        except ExchangeError:
            self._log.warning("cancel_failed", symbol=symbol, order_id=order_id)
            return False

    @staticmethod
    def measure_slippage(signal_price: float, fill_price: float, side: str) -> float:
        """Calculate slippage percentage (signed).

        Positive = unfavourable (paid more / received less).
        Negative = favourable (got a better price).
        """
        if signal_price <= 0:
            return 0.0
        if side == "buy":
            return (fill_price - signal_price) / signal_price * 100
        return (signal_price - fill_price) / signal_price * 100

    # ── entry flow (private) ─────────────────────────────────────

    async def _execute_entry(
        self,
        order_id: str,
        order_dict: dict,
        symbol: str,
        side: str,
        quantity: float,
        signal_price: float,
    ) -> OrderResult:
        """Full entry flow: check → limit → poll → market fallback."""
        # 1. Pre-trade slippage check
        check = await self._guard.check_orderbook(symbol, side, quantity)
        if not check.can_trade:
            self._log.info("entry_rejected", symbol=symbol, reason=check.reason)
            order_dict["status"] = "rejected"
            await self._state.save_order(order_dict)
            return OrderResult(
                order_id=order_id, symbol=symbol, side=side,
                quantity=quantity, filled_quantity=0.0,
                fill_price=None, slippage_pct=None, status="rejected",
            )

        # Capture orderbook snapshot into context
        ctx = order_dict.get("context") or {}
        ctx["orderbook"] = {
            "mid_price": check.mid_price,
            "expected_fill_price": check.expected_fill_price,
            "expected_slippage_pct": check.expected_slippage_pct,
            "spread_pct": check.spread_pct,
        }
        # Fetch full depth for debugging
        try:
            book = await self._client.fetch_orderbook(symbol, limit=5)
            ctx["orderbook"]["depth_top5"] = {
                "bids": book.get("bids", [])[:5],
                "asks": book.get("asks", [])[:5],
            }
        except Exception:  # noqa: BLE001
            pass  # non-critical — best-effort enrichment

        # 2. Place limit order
        limit_price = _calc_limit_price(check.mid_price, side)
        result = await self._client.place_limit_order(symbol, side, quantity, limit_price)

        order_dict["price"] = limit_price
        exchange_order_id = result["order_id"]
        order_dict["id"] = exchange_order_id

        ctx.setdefault("execution", {})
        ctx["execution"]["was_limit_attempted"] = True
        ctx["execution"]["exchange_order_id"] = exchange_order_id
        limit_placed_at = _now_ms()

        # 3. Poll for fill
        filled = await self._poll_for_fill(symbol, exchange_order_id)
        fill_latency_ms = _now_ms() - limit_placed_at
        ctx["execution"]["fill_latency_ms"] = fill_latency_ms

        if filled["status"] == "filled":
            fill_price = filled["avg_fill_price"] or limit_price
            filled_qty = filled["filled"]
            slippage = self.measure_slippage(signal_price, fill_price, side)
            self._guard.record_actual_slippage(symbol, signal_price, fill_price)
            commission = _calc_commission(filled_qty, fill_price, self._commission_pct)

            ctx["execution"]["fell_back_to_market"] = False
            ctx["execution"]["limit_wait_seconds"] = fill_latency_ms / 1000.0
            order_dict["context"] = ctx

            self._log.info(
                "limit_filled", symbol=symbol, side=side,
                fill_price=fill_price, slippage_pct=f"{slippage:.4f}",
            )
            await self._save_filled_order(
                order_dict, "filled", filled_qty, fill_price, slippage, commission,
            )
            return OrderResult(
                order_id=exchange_order_id, symbol=symbol, side=side,
                quantity=quantity, filled_quantity=filled_qty,
                fill_price=fill_price, slippage_pct=slippage,
                status="filled", commission=commission,
                exchange_order_id=exchange_order_id, context=ctx,
            )

        # 4. Partial fill on limit — keep what we have
        if filled["status"] == "partial" and filled["filled"] > 0:
            await self._client.cancel_order(symbol, exchange_order_id)
            fill_price = filled["avg_fill_price"] or limit_price
            filled_qty = filled["filled"]
            slippage = self.measure_slippage(signal_price, fill_price, side)
            self._guard.record_actual_slippage(symbol, signal_price, fill_price)
            commission = _calc_commission(filled_qty, fill_price, self._commission_pct)

            ctx["execution"]["fell_back_to_market"] = False
            ctx["execution"]["partial_fill"] = True
            order_dict["context"] = ctx

            self._log.info(
                "limit_partial", symbol=symbol, side=side,
                filled_qty=filled_qty, fill_price=fill_price,
            )
            await self._save_filled_order(
                order_dict, "partial", filled_qty, fill_price, slippage, commission,
            )
            return OrderResult(
                order_id=exchange_order_id, symbol=symbol, side=side,
                quantity=quantity, filled_quantity=filled_qty,
                fill_price=fill_price, slippage_pct=slippage,
                status="partial", commission=commission,
                exchange_order_id=exchange_order_id, context=ctx,
            )

        # 5. Limit not filled — cancel and fall back to market
        ctx["execution"]["fell_back_to_market"] = True
        ctx["execution"]["limit_wait_seconds"] = self._limit_wait
        self._log.info("limit_timeout_fallback", symbol=symbol)
        await self._client.cancel_order(symbol, exchange_order_id)
        return await self._market_fallback(
            order_dict, symbol, side, quantity, signal_price, ctx,
        )

    async def _poll_for_fill(
        self, symbol: str, order_id: str
    ) -> dict:
        """Poll exchange for order status until filled or timed out."""
        elapsed = 0.0
        while elapsed < self._limit_wait:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

            status = await self._client.fetch_order_status(symbol, order_id)
            mapped = _map_ccxt_status(status["status"])

            if mapped == "filled":
                return {
                    "status": "filled",
                    "filled": status["filled"],
                    "avg_fill_price": status["avg_fill_price"],
                }
            if mapped == "partial" and status["filled"] > 0:
                return {
                    "status": "partial",
                    "filled": status["filled"],
                    "avg_fill_price": status["avg_fill_price"],
                }
            if mapped in ("cancelled", "rejected"):
                return {"status": mapped, "filled": 0.0, "avg_fill_price": None}

        # Timed out — caller decides what to do
        return {"status": "timeout", "filled": 0.0, "avg_fill_price": None}

    async def _market_fallback(
        self,
        order_dict: dict,
        symbol: str,
        side: str,
        quantity: float,
        signal_price: float,
        ctx: dict,
    ) -> OrderResult:
        """Fall back to market order after limit timeout."""
        market_start = _now_ms()
        result = await self._client.place_market_order(symbol, side, quantity)

        market_order_id = result["order_id"]
        order_dict["id"] = market_order_id
        order_dict["order_type"] = "market"

        fill_price = result.get("avg_fill_price")
        filled_qty = result.get("quantity", quantity)

        if fill_price is None or filled_qty <= 0:
            order_dict["status"] = "error"
            order_dict["context"] = ctx
            await self._state.save_order(order_dict)
            return OrderResult(
                order_id=market_order_id, symbol=symbol, side=side,
                quantity=quantity, filled_quantity=0.0,
                fill_price=None, slippage_pct=None, status="error",
            )

        slippage = self.measure_slippage(signal_price, fill_price, side)
        self._guard.record_actual_slippage(symbol, signal_price, fill_price)
        commission = _calc_commission(filled_qty, fill_price, self._commission_pct)

        ctx.setdefault("execution", {})
        ctx["execution"]["exchange_order_id"] = market_order_id
        ctx["execution"]["market_fill_latency_ms"] = _now_ms() - market_start
        order_dict["context"] = ctx

        self._log.info(
            "market_fallback_filled", symbol=symbol, side=side,
            fill_price=fill_price, slippage_pct=f"{slippage:.4f}",
        )
        await self._save_filled_order(
            order_dict, "filled", filled_qty, fill_price, slippage, commission,
        )
        return OrderResult(
            order_id=market_order_id, symbol=symbol, side=side,
            quantity=quantity, filled_quantity=filled_qty,
            fill_price=fill_price, slippage_pct=slippage,
            status="filled", commission=commission,
            exchange_order_id=market_order_id, context=ctx,
        )

    # ── exit flow (private) ──────────────────────────────────────

    async def _execute_market_order(self, order_dict: dict) -> OrderResult:
        """Execute a market order for exits."""
        symbol = order_dict["symbol"]
        side = order_dict["side"]
        quantity = order_dict["quantity"]

        result = await self._client.place_market_order(symbol, side, quantity)

        order_id = result["order_id"]
        order_dict["id"] = order_id

        fill_price = result.get("avg_fill_price")
        filled_qty = result.get("quantity", quantity)

        if fill_price is None or filled_qty <= 0:
            order_dict["status"] = "error"
            await self._state.save_order(order_dict)
            return OrderResult(
                order_id=order_id, symbol=symbol, side=side,
                quantity=quantity, filled_quantity=0.0,
                fill_price=None, slippage_pct=None, status="error",
            )

        # For exits, slippage compares fill to last known price (no signal_price)
        slippage = 0.0
        commission = _calc_commission(filled_qty, fill_price, self._commission_pct)

        self._log.info(
            "exit_filled", symbol=symbol, side=side,
            fill_price=fill_price, commission=commission,
        )
        await self._save_filled_order(
            order_dict, "filled", filled_qty, fill_price, slippage, commission,
        )
        return OrderResult(
            order_id=order_id, symbol=symbol, side=side,
            quantity=quantity, filled_quantity=filled_qty,
            fill_price=fill_price, slippage_pct=slippage,
            status="filled", commission=commission,
        )

    # ── persistence helpers (private) ────────────────────────────

    async def _save_filled_order(
        self,
        order_dict: dict,
        status: str,
        filled_quantity: float,
        fill_price: float,
        slippage_pct: float,
        commission: float,
    ) -> None:
        """Persist a filled/partial order via state_manager."""
        order_dict["status"] = status
        order_dict["filled_quantity"] = filled_quantity
        order_dict["avg_fill_price"] = fill_price
        order_dict["slippage_pct"] = slippage_pct
        order_dict["commission"] = commission
        await self._state.save_order(order_dict)

    async def _finalize_error(self, order_dict: dict, error: str) -> OrderResult:
        """Save error order and return error OrderResult."""
        order_dict["status"] = "error"
        try:
            await self._state.save_order(order_dict)
        except Exception:  # noqa: BLE001
            self._log.warning("failed_to_save_error_order", order_id=order_dict["id"])
        return OrderResult(
            order_id=order_dict["id"],
            symbol=order_dict["symbol"],
            side=order_dict["side"],
            quantity=order_dict["quantity"],
            filled_quantity=0.0,
            fill_price=None,
            slippage_pct=None,
            status="error",
        )


def _gen_id() -> str:
    """Generate a unique order tracking ID."""
    return uuid.uuid4().hex[:16]


def _now_ms() -> int:
    """Current time in milliseconds since epoch."""
    import time
    return int(time.monotonic() * 1000)
