"""Enhanced strategy — async wrapper over pure signal functions.

Delegates to enhanced_signals module for actual signal computation.
Implements BaseStrategy interface so the backtest engine can use it
as a drop-in replacement for MomentumStrategy.
"""

from __future__ import annotations

from typing import Optional

from .base_strategy import BaseStrategy, StrategyConfig
from .enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal
from ..core.models.signal import Signal, SignalDirection
from ..core.models.candle import CandleSeries
from ..core.models.market_data import MarketData


class EnhancedStrategy(BaseStrategy):
    """Multi-signal strategy combining breakout, mean-reversion, and trend.

    Uses union logic: any sub-signal type can trigger a trade.
    Tracks signal sources in metadata for diagnostics.
    """

    def __init__(self, config: Optional[EnhancedSignalConfig] = None):
        super().__init__(StrategyConfig(name="enhanced"))
        self.enhanced_config = config or EnhancedSignalConfig()
        self.diagnostics: dict[str, int] = {
            "total_evaluated": 0,
            "signals_produced": 0,
        }

    async def generate_signal(
        self,
        symbol: str,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> Signal:
        """Generate enhanced trading signal via pure functions."""
        self.diagnostics["total_evaluated"] += 1
        signal = generate_enhanced_signal(symbol, candles, self.enhanced_config)

        if signal.direction != SignalDirection.NEUTRAL:
            self.diagnostics["signals_produced"] += 1

        return signal

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        risk_per_trade: float = 0.02,
    ) -> float:
        """Calculate position size from signal strength and risk."""
        if signal.direction == SignalDirection.NEUTRAL or signal.price <= 0:
            return 0.0
        risk_amount = portfolio_value * risk_per_trade
        adjusted_risk = risk_amount * max(signal.strength, 0.3)
        return adjusted_risk / signal.price
