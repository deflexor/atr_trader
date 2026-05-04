"""Equity snapshots, trade PnL recording, and performance reporting."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

import structlog

from ..core.models.position import Position

logger = structlog.get_logger(__name__)


def _calc_pnl(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    """Calculate raw PnL mirroring backtest engine logic."""
    if side == "long":
        return (exit_price - entry_price) * qty
    return (entry_price - exit_price) * qty


def _unrealized_pnl(positions: list[Position]) -> float:
    """Sum unrealized PnL across all positions."""
    return sum(p.unrealized_pnl for p in positions)


def _sharpe_approx(pnls: list[float]) -> float:
    """Simplified Sharpe: mean / std, annualized by sqrt(252)."""
    if len(pnls) < 2:
        return 0.0
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
    if variance == 0:
        return 0.0
    import math

    return (mean / math.sqrt(variance)) * math.sqrt(252)


class PnlTracker:
    """Equity snapshots, trade PnL recording, and performance reporting."""

    def __init__(self, state_manager: Any) -> None:
        self._state = state_manager
        self._daily_start_equity: float = 0.0
        self._total_pnl: float = 0.0
        self._trade_count: int = 0
        self._win_count: int = 0
        self._trade_pnls: list[float] = []

    async def record_equity_snapshot(
        self, positions: list[Position], cash: float
    ) -> None:
        """Record current equity state via state manager."""
        upnl = _unrealized_pnl(positions)
        total_equity = cash + upnl
        daily_pnl = total_equity - self._daily_start_equity

        await self._state.save_equity_snapshot(
            total_equity=total_equity,
            cash=cash,
            unrealized_pnl=upnl,
            open_positions=len(positions),
            daily_pnl=daily_pnl,
            total_pnl=self._total_pnl,
        )

    async def record_trade_closed(
        self,
        position: Position,
        exit_price: float,
        exit_reason: str,
        commission: float = 0.0,
        slippage: float = 0.0,
        market_context: Optional[dict] = None,
    ) -> None:
        """Record a closed trade and update internal stats.

        market_context: dict with candle/ATR/position state for debugging.
            e.g. {"candle": {...}, "atr": 150.0, "position_state": {...}}
        """
        raw_pnl = _calc_pnl(
            position.side, position.avg_entry_price, exit_price, position.total_quantity
        )
        net_pnl = raw_pnl - commission - slippage

        now = datetime.utcnow()
        created = position.created_at
        duration = int((now - created).total_seconds()) if created else 0

        # Build context with position exit state
        ctx = market_context or {}
        ctx.setdefault("position_state", {})
        ctx["position_state"].update({
            "highest_price_reached": position.highest_price,
            "lowest_price_reached": position.lowest_price if position.lowest_price != float("inf") else None,
            "trailing_stop_level": position.trailing_stop,
            "trailing_activated": position.trailing_activated,
            "trailing_atr_multiplier": position.trailing_atr_multiplier,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
        })

        trade = {
            "id": str(uuid.uuid4()),
            "position_id": position.id,
            "symbol": position.symbol,
            "side": position.side,
            "entry_price": position.avg_entry_price,
            "exit_price": exit_price,
            "quantity": position.total_quantity,
            "pnl": net_pnl,
            "commission": commission,
            "slippage": slippage,
            "exit_reason": exit_reason,
            "duration_seconds": duration,
            "context": ctx,
            "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
            "closed_at": now.isoformat(),
        }

        await self._state.save_trade(trade)

        self._total_pnl += net_pnl
        self._trade_count += 1
        self._trade_pnls.append(net_pnl)
        if net_pnl > 0:
            self._win_count += 1

    async def log_daily_summary(
        self, positions: list[Position], cash: float
    ) -> None:
        """Log daily performance summary."""
        upnl = _unrealized_pnl(positions)
        total_equity = cash + upnl
        daily_pnl = total_equity - self._daily_start_equity
        win_rate = self._win_count / self._trade_count if self._trade_count > 0 else 0.0

        logger.info(
            "daily_summary",
            total_equity=round(total_equity, 2),
            daily_pnl=round(daily_pnl, 2),
            win_rate=round(win_rate, 4),
            trades=self._trade_count,
            open_positions=len(positions),
        )

    def get_performance_stats(self) -> dict[str, Any]:
        """Return performance statistics dict."""
        win_rate = self._win_count / self._trade_count if self._trade_count > 0 else 0.0
        avg_pnl = self._total_pnl / self._trade_count if self._trade_count > 0 else 0.0

        return {
            "total_trades": self._trade_count,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "total_pnl": self._total_pnl,
            "sharpe_approximation": _sharpe_approx(self._trade_pnls),
        }

    def reset_daily(self, equity: float) -> None:
        """Reset daily PnL tracker. Called at start of each trading day."""
        self._daily_start_equity = equity
