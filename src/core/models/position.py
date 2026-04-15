"""Position model for tracking open trades."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Position:
    """Position model for tracking open trades.

    Supports pyramid entries (multiple entries per position)
    and tracks entry price, quantity, and PnL.

    """

    id: Optional[str] = None
    symbol: str = ""
    exchange: str = ""
    side: str = "long"  # long or short
    quantity: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    entries: list[dict] = field(default_factory=list)  # [{price, quantity, timestamp}]
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    strategy_id: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    @property
    def cost_basis(self) -> float:
        """Calculate total cost basis for all entries."""
        return sum(e["price"] * e["quantity"] for e in self.entries)

    @property
    def total_quantity(self) -> float:
        """Calculate total quantity across all entries."""
        return sum(e["quantity"] for e in self.entries)

    @property
    def avg_entry_price(self) -> float:
        """Calculate average entry price weighted by quantity."""
        if self.total_quantity == 0:
            return 0.0
        return self.cost_basis / self.total_quantity

    @property
    def unrealized_pnl(self) -> float:
        """Calculate unrealized PnL."""
        if self.side == "long":
            return (self.current_price - self.avg_entry_price) * self.total_quantity
        else:
            return (self.avg_entry_price - self.current_price) * self.total_quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        """Calculate unrealized PnL as percentage of cost basis."""
        if self.cost_basis == 0:
            return 0.0
        return self.unrealized_pnl / self.cost_basis * 100

    def add_entry(self, price: float, quantity: float) -> None:
        """Add a pyramid entry to the position."""
        self.entries.append(
            {
                "price": price,
                "quantity": quantity,
                "timestamp": datetime.utcnow(),
            }
        )
        self.updated_at = datetime.utcnow()

    def update_price(self, price: float) -> None:
        """Update current market price and recalculate PnL."""
        self.current_price = price
        self.updated_at = datetime.utcnow()

    def is_stop_triggered(self) -> bool:
        """Check if stop loss is triggered."""
        if self.stop_loss is None:
            return False
        if self.side == "long":
            return self.current_price <= self.stop_loss
        else:
            return self.current_price >= self.stop_loss

    def is_tp_triggered(self) -> bool:
        """Check if take profit is triggered."""
        if self.take_profit is None:
            return False
        if self.side == "long":
            return self.current_price >= self.take_profit
        else:
            return self.current_price <= self.take_profit
