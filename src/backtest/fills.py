"""Fill simulation with realistic slippage modeling.

Models order book depth and volume-based slippage for realistic
backtest fill prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import random


@dataclass
class SlippageModel:
    """Slippage model configuration."""

    type: str = "volume"  # volume or orderbook
    base_slippage: float = 0.0005  # 0.05% base slippage
    volume_factor: float = 0.001  # Volume-based slippage multiplier
    max_slippage: float = 0.01  # 1% max slippage cap


class FillSimulator:
    """Simulates realistic order fills with slippage.

    Provides two slippage models:
    1. volume: Slippage based on trade volume relative to average
    2. orderbook: Slippage based on order book depth (if available)
    """

    def __init__(
        self,
        slippage_type: str = "volume",
        slippage_factor: float = 0.0005,
        max_slippage: float = 0.01,
    ):
        self.slippage_type = slippage_type
        self.slippage_factor = slippage_factor
        self.max_slippage = max_slippage

        # Track volume for dynamic slippage
        self.avg_volume: float = 0.0
        self.volume_count: int = 0

    def update_volume(self, volume: float) -> None:
        """Update average volume tracking."""
        if volume > 0:
            self.volume_count += 1
            # Rolling average
            self.avg_volume = (
                self.avg_volume * (self.volume_count - 1) + volume
            ) / self.volume_count

    def calculate_fill_price(
        self,
        target_price: float,
        is_buy: bool,
        volume: float = 0.0,
        order_book_depth: Optional[list[tuple[float, float]]] = None,
    ) -> float:
        """Calculate realistic fill price with slippage.

        Args:
            target_price: Target execution price
            is_buy: True if buy order, False if sell
            volume: Trade volume (for volume-based slippage)
            order_book_depth: Optional order book [(price, size)] for depth-based slippage

        Returns:
            Realistic fill price including slippage

        """
        if self.slippage_type == "orderbook" and order_book_depth:
            return self._orderbook_slippage(target_price, is_buy, order_book_depth)
        else:
            return self._volume_slippage(target_price, is_buy, volume)

    def _volume_slippage(
        self,
        target_price: float,
        is_buy: bool,
        volume: float,
    ) -> float:
        """Calculate slippage based on volume.

        Higher volume relative to average -> more slippage.
        """
        # Update volume tracking
        if volume > 0:
            self.update_volume(volume)

        # Calculate volume ratio (current volume vs average)
        volume_ratio = volume / self.avg_volume if self.avg_volume > 0 else 1.0

        # Slippage increases with volume ratio
        slippage = self.slippage_factor * (1 + volume_ratio)

        # Cap slippage
        slippage = min(slippage, self.max_slippage)

        # Add some randomness (10% of slippage is random)
        random_factor = 1.0 + random.uniform(-0.1, 0.1) * slippage
        slippage = slippage * random_factor

        # Apply slippage
        if is_buy:
            return target_price * (1 + slippage)
        else:
            return target_price * (1 - slippage)

    def _orderbook_slippage(
        self,
        target_price: float,
        is_buy: bool,
        order_book: list[tuple[float, float]],
    ) -> float:
        """Calculate slippage based on order book depth.

        Traverses order book and calculates average fill price
        based on available liquidity.
        """
        if not order_book:
            return self._volume_slippage(target_price, is_buy, 0)

        cumulative_volume = 0.0
        fill_price_sum = 0.0
        remaining_volume = 1.0  # Assume normalized volume

        if is_buy:
            # Traverse asks from low to high
            asks = sorted([(price, size) for price, size in order_book if price >= target_price])
            for price, size in asks:
                fill = min(size, remaining_volume)
                fill_price_sum += price * fill
                cumulative_volume += fill
                remaining_volume -= fill
                if remaining_volume <= 0:
                    break
        else:
            # Traverse bids from high to low
            bids = sorted(
                [(price, size) for price, size in order_book if price <= target_price], reverse=True
            )
            for price, size in bids:
                fill = min(size, remaining_volume)
                fill_price_sum += price * fill
                cumulative_volume += fill
                remaining_volume -= fill
                if remaining_volume <= 0:
                    break

        if cumulative_volume > 0:
            return fill_price_sum / cumulative_volume
        return target_price

    def estimate_market_impact(
        self,
        order_size: float,
        avg_daily_volume: float,
        is_buy: bool,
    ) -> float:
        """Estimate market impact of a large order.

        Uses square root model: impact = sigma * sqrt(Q/ADV)
        where Q is order size and ADV is average daily volume.

        Args:
            order_size: Order size
            avg_daily_volume: Average daily volume
            is_buy: True if buy order

        Returns:
            Estimated market impact as fraction of price

        """
        if avg_daily_volume == 0:
            return 0

        # Square root market impact model
        participation_rate = order_size / avg_daily_volume
        impact = 0.1 * (participation_rate**0.5)  # 10% base impact at 100% participation

        # Cap impact at max slippage
        impact = min(impact, self.max_slippage)

        return impact

    def calculate_fill_with_impact(
        self,
        target_price: float,
        order_size: float,
        avg_daily_volume: float,
        is_buy: bool,
    ) -> float:
        """Calculate fill price including market impact.

        Args:
            target_price: Target price
            order_size: Order size
            avg_daily_volume: Average daily volume
            is_buy: True if buy order

        Returns:
            Fill price with market impact

        """
        impact = self.estimate_market_impact(order_size, avg_daily_volume, is_buy)

        if is_buy:
            return target_price * (1 + impact)
        else:
            return target_price * (1 - impact)
