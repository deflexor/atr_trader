"""Portfolio model for tracking overall trading account state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Portfolio:
    """Portfolio model for tracking overall trading account state.

    Tracks total equity, positions, cash balance, and performance metrics.

    """

    initial_capital: float = 0.0
    current_capital: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    positions: list[dict] = field(default_factory=list)  # Position dicts
    daily_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    updated_at: datetime = field(default_factory=datetime.utcnow)
    equity_curve: list[dict] = field(default_factory=list)  # [{timestamp, equity}]

    @property
    def available_capital(self) -> float:
        """Calculate available capital (current - margin used)."""
        margin_used = sum(
            p.get("quantity", 0) * p.get("entry_price", 0) * 0.1 for p in self.positions
        )
        return max(0.0, self.current_capital - margin_used)

    @property
    def exposure(self) -> float:
        """Calculate total portfolio exposure."""
        return sum(abs(p.get("quantity", 0) * p.get("entry_price", 0)) for p in self.positions)

    @property
    def leverage(self) -> float:
        """Calculate current leverage (exposure / equity)."""
        if self.current_capital == 0:
            return 0.0
        return self.exposure / self.current_capital

    def update_equity(self) -> None:
        """Update equity curve with current state."""
        self.equity_curve.append(
            {
                "timestamp": datetime.utcnow(),
                "equity": self.current_capital,
                "positions_count": len(self.positions),
            }
        )
        self.updated_at = datetime.utcnow()

    def to_dict(self) -> dict:
        """Convert portfolio to dictionary for storage."""
        return {
            "initial_capital": self.initial_capital,
            "current_capital": self.current_capital,
            "total_pnl": self.total_pnl,
            "total_pnl_pct": self.total_pnl_pct,
            "positions": self.positions,
            "daily_pnl": self.daily_pnl,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "sharpe_ratio": self.sharpe_ratio,
            "updated_at": self.updated_at.isoformat(),
        }
