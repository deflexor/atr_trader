"""Base strategy class for all trading strategies.

Provides common interface and functionality for all strategies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any
import logging

from ..core.models.signal import Signal, SignalDirection
from ..core.models.candle import Candle, CandleSeries
from ..core.models.market_data import MarketData

logger = logging.getLogger(__name__)


@dataclass
class StrategyConfig:
    """Base configuration for all strategies."""

    name: str = ""
    enabled: bool = True
    pyramid_entries: int = 2
    entry_spacing: float = 0.005
    volatility_filter: float = 0.02
    min_signal_strength: float = 0.6
    max_positions: int = 5


@dataclass
class StrategyState:
    """Runtime state for a strategy."""

    is_running: bool = False
    last_signal_time: Optional[datetime] = None
    signals_generated: int = 0
    active_positions: list[str] = field(default_factory=list)  # position IDs


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    All strategies must implement the generate_signal method.
    Strategies can optionally override lifecycle methods.

    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.state = StrategyState()
        self.logger = logging.getLogger(f"{__name__}.{config.name}")

    @abstractmethod
    async def generate_signal(
        self,
        symbol: str,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> Signal:
        """Generate trading signal based on market data.

        Args:
            symbol: Trading pair symbol
            candles: Historical candle data
            market_data: Current market data (optional)

        Returns:
            Signal with direction, strength, and metadata

        """
        pass

    @abstractmethod
    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        risk_per_trade: float = 0.02,
    ) -> float:
        """Calculate position size based on signal and risk parameters.

        Args:
            signal: Generated trading signal
            portfolio_value: Total portfolio value
            risk_per_trade: Risk percentage per trade (default 2%)

        Returns:
            Position size in base currency

        """
        pass

    def should_entry(self, candles: CandleSeries) -> bool:
        """Check if market conditions are suitable for entry.

        Default implementation checks volatility filter.
        Override for custom entry conditions.

        """
        if len(candles.candles) < 20:
            return False

        # Calculate volatility ( ATR or standard deviation )
        recent = candles.candles[-20:]
        returns = []
        for i in range(1, len(recent)):
            returns.append((recent[i].close - recent[i - 1].close) / recent[i - 1].close)

        if not returns:
            return False

        import statistics

        volatility = statistics.stdev(returns)

        # Check against volatility filter
        return volatility <= self.config.volatility_filter

    def should_exit(
        self,
        position: dict,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> bool:
        """Check if position should be exited.

        Default implementation checks stop loss and take profit.
        Override for custom exit conditions.

        """
        if not candles.candles:
            return False

        current_price = candles.candles[-1].close
        entry_price = position.get("entry_price", 0)
        side = position.get("side", "long")

        stop_loss = position.get("stop_loss")
        take_profit = position.get("take_profit")

        if side == "long":
            if stop_loss and current_price <= stop_loss:
                return True
            if take_profit and current_price >= take_profit:
                return True
        else:  # short
            if stop_loss and current_price >= stop_loss:
                return True
            if take_profit and current_price <= take_profit:
                return True

        return False

    async def on_signal(self, signal: Signal) -> None:
        """Callback when signal is generated.

        Can be overridden for custom signal handling.

        """
        self.state.last_signal_time = datetime.utcnow()
        self.state.signals_generated += 1
        self.logger.info(
            f"Signal generated: {signal.direction.value} {signal.symbol} strength={signal.strength:.2f}"
        )

    async def on_fill(self, order_id: str, fill_price: float, quantity: float) -> None:
        """Callback when order is filled.

        Can be overridden for custom fill handling.

        """
        self.logger.info(f"Order filled: {order_id} @ {fill_price} x {quantity}")

    async def on_error(self, error: Exception) -> None:
        """Callback when strategy encounters error.

        Default logs the error. Override for custom error handling.

        """
        self.logger.error(f"Strategy error: {type(error).__name__}: {error}")

    def start(self) -> None:
        """Start the strategy."""
        self.state.is_running = True
        self.logger.info(f"Strategy {self.config.name} started")

    def stop(self) -> None:
        """Stop the strategy."""
        self.state.is_running = False
        self.logger.info(f"Strategy {self.config.name} stopped")

    def get_state(self) -> Dict[str, Any]:
        """Get current strategy state for monitoring."""
        return {
            "name": self.config.name,
            "enabled": self.config.enabled,
            "is_running": self.state.is_running,
            "signals_generated": self.state.signals_generated,
            "last_signal_time": self.state.last_signal_time.isoformat()
            if self.state.last_signal_time
            else None,
            "active_positions": self.state.active_positions,
        }
