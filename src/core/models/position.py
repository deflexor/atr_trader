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

    # Trailing stop state
    highest_price: float = 0.0  # Track highest price since entry (for longs)
    lowest_price: float = float("inf")  # Track lowest price since entry (for shorts)
    trailing_stop: Optional[float] = None  # Active trailing stop level
    trailing_activated: bool = False  # Whether trailing stop is active
    trailing_atr_multiplier: float = 2.5  # Per-position ATR multiplier (can vary by volatility)

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

    def reduce_entries(self, fraction: float) -> tuple[float, float]:
        """Remove FIFO entries to reduce position by a fraction.

        Removes entries from the front of the list (earliest first).
        If the last entry to remove is partial, splits it.

        Args:
            fraction: Fraction of total quantity to remove (0.0-1.0).

        Returns:
            (closed_quantity, closed_entry_value) — quantity and cost basis
            of the removed entries. Used for PnL calculation and capital return.
        """
        if fraction <= 0.0 or not self.entries:
            return 0.0, 0.0

        total_qty = self.total_quantity
        target_close = total_qty * min(fraction, 1.0)
        closed_qty = 0.0
        closed_value = 0.0

        while target_close > 0.0 and self.entries:
            entry = self.entries[0]
            entry_qty = entry["quantity"]
            entry_price = entry["price"]

            if entry_qty <= target_close:
                # Remove entire entry
                self.entries.pop(0)
                closed_qty += entry_qty
                closed_value += entry_qty * entry_price
                target_close -= entry_qty
            else:
                # Partial remove: split the entry
                entry["quantity"] = entry_qty - target_close
                closed_qty += target_close
                closed_value += target_close * entry_price
                target_close = 0.0

        self.updated_at = datetime.utcnow()
        return closed_qty, closed_value

    def update_price(self, price: float) -> None:
        """Update current market price and track extremes for trailing stops."""
        self.current_price = price
        if self.side == "long":
            if price > self.highest_price:
                self.highest_price = price
        else:
            if price < self.lowest_price:
                self.lowest_price = price
        self.updated_at = datetime.utcnow()

    def update_trailing_stop(
        self,
        activation_atr: float,
        distance_atr: float,
        atr_value: float,
    ) -> None:
        """Update trailing stop based on price movement and ATR.

        Activation: price must have moved activation_atr * ATR in our favor.
        Distance: trail at distance_atr * ATR behind the extreme.
        Uses self.trailing_atr_multiplier if set (from volatility adaptation).
        """
        if atr_value <= 0:
            return

        # Use per-position multiplier if set, otherwise fall back to passed values
        mult = self.trailing_atr_multiplier if self.trailing_atr_multiplier > 0 else activation_atr
        activation_threshold = mult * atr_value
        trail_distance = mult * atr_value

        if self.side == "long":
            # Activate when price moved enough above entry
            if not self.trailing_activated:
                if self.highest_price - self.avg_entry_price >= activation_threshold:
                    self.trailing_activated = True

            if self.trailing_activated:
                new_trail = self.highest_price - trail_distance
                # Only move trailing stop UP, never down
                if self.trailing_stop is None or new_trail > self.trailing_stop:
                    self.trailing_stop = new_trail
        else:
            # Short: activate when price dropped enough below entry
            if not self.trailing_activated:
                if self.avg_entry_price - self.lowest_price >= activation_threshold:
                    self.trailing_activated = True

            if self.trailing_activated:
                new_trail = self.lowest_price + trail_distance
                # Only move trailing stop DOWN, never up
                if self.trailing_stop is None or new_trail < self.trailing_stop:
                    self.trailing_stop = new_trail

    def is_trailing_triggered(self) -> bool:
        """Check if trailing stop is triggered."""
        if not self.trailing_activated or self.trailing_stop is None:
            return False
        if self.side == "long":
            return self.current_price <= self.trailing_stop
        else:
            return self.current_price >= self.trailing_stop

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
