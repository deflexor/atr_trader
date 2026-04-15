"""Order model for trade execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderType(Enum):
    """Order type enumeration."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderSide(Enum):
    """Order side enumeration."""

    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    """Order status enumeration."""

    PENDING = "pending"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class OrderFill:
    """Individual order fill."""

    price: float
    quantity: float
    fee: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Order:
    """Order model for trade execution.

    Tracks order lifecycle from creation through filling or cancellation.
    Supports partial fills and multiple fill tracking.

    """

    id: Optional[str] = None
    symbol: str = ""
    exchange: str = ""
    order_type: OrderType = OrderType.MARKET
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    avg_fill_price: Optional[float] = None
    fills: list[OrderFill] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    strategy_id: Optional[str] = None
    notes: str = ""

    @property
    def remaining_quantity(self) -> float:
        """Calculate remaining quantity to be filled."""
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def fill_rate(self) -> float:
        """Calculate fill rate as percentage of total quantity."""
        return self.filled_quantity / self.quantity if self.quantity > 0 else 0.0

    def add_fill(self, fill: OrderFill) -> None:
        """Add a fill to the order and update status."""
        self.fills.append(fill)
        self.filled_quantity += fill.quantity
        self.updated_at = datetime.utcnow()

        if self.filled_quantity >= self.quantity:
            self.status = OrderStatus.FILLED
        elif self.filled_quantity > 0:
            self.status = OrderStatus.PARTIAL

    def cancel(self) -> None:
        """Cancel the order."""
        if self.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL):
            self.status = OrderStatus.CANCELLED
            self.updated_at = datetime.utcnow()
