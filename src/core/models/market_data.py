"""Market data model for real-time price and order book data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class MarketData:
    """Immutable market data snapshot.

    Represents a single point-in-time market state with best bid/ask
    and volume information.

    """

    symbol: str
    exchange: str
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    last_price: Optional[float] = None
    volume: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def mid_price(self) -> float:
        """Calculate mid price between bid and ask."""
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        """Calculate bid-ask spread."""
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Calculate spread as percentage of mid price."""
        return self.spread / self.mid_price if self.mid_price > 0 else 0.0

    @property
    def cache_key(self) -> str:
        """Create cache key for this market data."""
        return f"{self.exchange}:{self.symbol}"

    def with_price(self, bid: float, ask: float) -> MarketData:
        """Create new instance with updated prices."""
        return MarketData(
            symbol=self.symbol,
            exchange=self.exchange,
            bid=bid,
            ask=ask,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            last_price=self.last_price,
            volume=self.volume,
            timestamp=datetime.utcnow(),
        )
