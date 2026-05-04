"""Pre-trade orderbook slippage estimation with adaptive sizing.

Analyzes orderbook depth before placing orders, tracks actual slippage
over a rolling window, and adjusts position sizing accordingly.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)

MAX_LEVELS_TO_WALK = 3
SPREAD_HISTORY_SIZE = 10


@dataclass(frozen=True)
class SlippageCheck:
    """Immutable result of an orderbook slippage check."""

    symbol: str
    side: str
    mid_price: float
    expected_fill_price: float
    expected_slippage_pct: float
    spread_pct: float
    can_trade: bool
    reason: str


def _walk_book_side(
    levels: list[list[float]], quantity: float
) -> tuple[float, float]:
    """Walk top levels and return (weighted_fill_price, total_available).

    Returns weighted average price consuming up to MAX_LEVELS_TO_WALK levels.
    If quantity exceeds available liquidity, returns best-effort fill price.
    """
    cost = 0.0
    filled = 0.0
    for price, qty in levels[:MAX_LEVELS_TO_WALK]:
        take = min(quantity - filled, qty)
        cost += price * take
        filled += take
        if filled >= quantity:
            break
    if filled == 0:
        return 0.0, 0.0
    return cost / filled, filled


def _calc_slippage_pct(fill_price: float, mid_price: float, side: str) -> float:
    """Calculate slippage percentage (always positive).

    Buys: fill above mid → (fill - mid) / mid * 100
    Sells: fill below mid → (mid - fill) / mid * 100
    """
    if mid_price <= 0:
        return 0.0
    if side == "buy":
        return (fill_price - mid_price) / mid_price * 100
    return (mid_price - fill_price) / mid_price * 100


class SlippageGuard:
    """Pre-trade slippage estimation with adaptive sizing."""

    def __init__(
        self,
        exchange_client,
        max_slippage_pct: float = 0.10,
        max_spread_pct: float = 0.15,
        rolling_window: int = 20,
        blacklist_threshold: int = 3,
    ) -> None:
        self._client = exchange_client
        self.max_slippage_pct = max_slippage_pct
        self.max_spread_pct = max_spread_pct
        self.rolling_window = rolling_window
        self.blacklist_threshold = blacklist_threshold
        self._spread_history: dict[str, deque[float]] = {}
        self._slippage_history: dict[str, deque[float]] = {}
        self._log = logger.bind(component="slippage_guard")

    async def check_orderbook(
        self, symbol: str, side: str, quantity: float
    ) -> SlippageCheck:
        """Analyze orderbook before placing order."""
        if side not in ("buy", "sell"):
            return self._reject(symbol, side, 0.0, 0.0, f"invalid side: {side}")

        try:
            book = await self._client.fetch_orderbook(symbol, limit=10)
        except Exception as exc:
            self._log.warning("orderbook_fetch_failed", symbol=symbol, error=str(exc))
            return self._reject(symbol, side, 0.0, 0.0, f"orderbook fetch failed: {exc}")

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return self._reject(symbol, side, 0.0, 0.0, "empty orderbook")

        best_bid, best_ask = bids[0][0], asks[0][0]
        mid_price = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid_price * 100

        self._record_spread(symbol, spread_pct)

        if spread_pct > self.max_spread_pct:
            reason = f"spread {spread_pct:.4f}% > max {self.max_spread_pct}%"
            return SlippageCheck(
                symbol, side, mid_price, 0.0, 0.0, spread_pct, False, reason
            )

        levels = asks if side == "buy" else bids
        fill_price, available = _walk_book_side(levels, quantity)
        if available < quantity:
            fill_price, available = _walk_book_side(levels, available)

        expected_slippage = _calc_slippage_pct(fill_price, mid_price, side)

        if expected_slippage > self.max_slippage_pct:
            reason = f"expected slippage {expected_slippage:.4f}% > max {self.max_slippage_pct}%"
            return SlippageCheck(
                symbol, side, mid_price, fill_price, expected_slippage,
                spread_pct, False, reason,
            )

        if available < quantity:
            reason = f"insufficient liquidity: {available:.6f} < {quantity:.6f}"
            return SlippageCheck(
                symbol, side, mid_price, fill_price, expected_slippage,
                spread_pct, False, reason,
            )

        return SlippageCheck(
            symbol, side, mid_price, fill_price, expected_slippage,
            spread_pct, True, "",
        )

    def record_actual_slippage(
        self, symbol: str, signal_price: float, fill_price: float
    ) -> float:
        """Record actual slippage after fill. Returns slippage_pct."""
        if signal_price <= 0:
            return 0.0
        slippage_pct = abs(fill_price - signal_price) / signal_price * 100
        history = self._slippage_history.setdefault(
            symbol, deque(maxlen=self.rolling_window)
        )
        history.append(slippage_pct)
        if slippage_pct > self.max_slippage_pct:
            self._log.warning(
                "high_actual_slippage",
                symbol=symbol,
                slippage_pct=f"{slippage_pct:.4f}",
                signal_price=signal_price,
                fill_price=fill_price,
            )
        return slippage_pct

    def get_rolling_avg_slippage(self, symbol: str) -> float:
        """Average slippage over last N trades. 0.0 if no data."""
        history = self._slippage_history.get(symbol)
        if not history:
            return 0.0
        return sum(history) / len(history)

    def get_size_multiplier(self, symbol: str) -> float:
        """Position size multiplier based on rolling slippage."""
        avg = self.get_rolling_avg_slippage(symbol)
        if avg <= 0.05:
            return 1.0
        if avg > 0.08:
            return max(0.25, 0.08 / avg)
        return 1.0

    def is_blacklisted(self, symbol: str) -> bool:
        """True if last N spread checks all exceeded max_spread_pct."""
        history = self._spread_history.get(symbol)
        if not history or len(history) < self.blacklist_threshold:
            return False
        recent = list(history)[-self.blacklist_threshold:]
        if all(s > self.max_spread_pct for s in recent):
            self._log.warning("symbol_blacklisted", symbol=symbol)
            return True
        return False

    # ── private helpers ────────────────────────────────────────

    def _reject(
        self, symbol: str, side: str, mid: float, spread: float, reason: str
    ) -> SlippageCheck:
        return SlippageCheck(symbol, side, mid, 0.0, 0.0, spread, False, reason)

    def _record_spread(self, symbol: str, spread_pct: float) -> None:
        history = self._spread_history.setdefault(
            symbol, deque(maxlen=SPREAD_HISTORY_SIZE)
        )
        history.append(spread_pct)
