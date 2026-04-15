"""Momentum trading strategy.

Enters positions based on momentum indicators (RSI, MACD, price momentum).
Follows the trend - buys on strength, sells on weakness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import statistics

from .base_strategy import BaseStrategy, StrategyConfig
from ..core.models.signal import Signal, SignalDirection
from ..core.models.candle import CandleSeries
from ..core.models.market_data import MarketData


@dataclass
class MomentumConfig(StrategyConfig):
    """Configuration for momentum strategy."""

    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    momentum_threshold: float = 0.02


class MomentumStrategy(BaseStrategy):
    """Momentum-based trading strategy.

    Generates signals based on:
    - RSI (overbought/oversold)
    - MACD (trend direction)
    - Price momentum (rate of change)
    """

    def __init__(self, config: Optional[MomentumConfig] = None):
        super().__init__(config or MomentumConfig(name="momentum"))
        self.momentum_config = config or MomentumConfig(name="momentum")

    def calculate_rsi(self, prices: list[float], period: int = 14) -> Optional[float]:
        """Calculate RSI indicator."""
        if len(prices) < period + 1:
            return None

        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]

        avg_gain = statistics.mean(gains) if gains else 0
        avg_loss = statistics.mean(losses) if losses else 0

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def calculate_macd(
        self,
        prices: list[float],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Optional[tuple[float, float, float]]:
        """Calculate MACD (macd_line, signal_line, histogram)."""
        if len(prices) < slow + signal:
            return None

        # Calculate EMAs
        def ema(data: list[float], period: int) -> list[float]:
            if not data:
                return []
            multiplier = 2 / (period + 1)
            result = [data[0]]
            for price in data[1:]:
                result.append((price - result[-1]) * multiplier + result[-1])
            return result

        ema_fast = ema(prices, fast)
        ema_slow = ema(prices, slow)

        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(ema_slow))]
        signal_line = ema(macd_line, signal)

        if len(macd_line) < signal or len(signal_line) < 1:
            return None

        histogram = macd_line[-1] - signal_line[-1]
        return macd_line[-1], signal_line[-1], histogram

    def calculate_momentum(self, prices: list[float], period: int = 10) -> float:
        """Calculate price momentum as rate of change."""
        if len(prices) < period + 1:
            return 0.0
        return (prices[-1] - prices[-period]) / prices[-period]

    async def generate_signal(
        self,
        symbol: str,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> Signal:
        """Generate momentum-based trading signal."""
        if len(candles.candles) < 50:
            return Signal(
                symbol=symbol,
                exchange=candles.exchange or "kucoin",
                direction=SignalDirection.NEUTRAL,
                strength=0.0,
                confidence=0.0,
                strategy_id=self.config.name,
            )

        closes = candles.closes
        rsi = self.calculate_rsi(closes, self.momentum_config.rsi_period)
        macd = self.calculate_macd(
            closes,
            self.momentum_config.macd_fast,
            self.momentum_config.macd_slow,
            self.momentum_config.macd_signal,
        )
        momentum = self.calculate_momentum(closes, 10)

        # Calculate signal strength
        direction = SignalDirection.NEUTRAL
        strength = 0.0
        confidence = 0.0

        signals_triggered = 0
        total_indicators = 0

        # RSI analysis
        if rsi is not None:
            total_indicators += 1
            if rsi < self.momentum_config.rsi_oversold:
                signals_triggered += 1
            elif rsi > self.momentum_config.rsi_overbought:
                signals_triggered -= 1

        # MACD analysis
        if macd is not None:
            total_indicators += 1
            macd_line, signal_line, histogram = macd
            if histogram > 0 and macd_line > signal_line:
                signals_triggered += 1
            elif histogram < 0 and macd_line < signal_line:
                signals_triggered -= 1

        # Momentum analysis
        if abs(momentum) > self.momentum_config.momentum_threshold:
            total_indicators += 1
            if momentum > 0:
                signals_triggered += 1
            else:
                signals_triggered -= 1

        # Calculate direction and strength
        if signals_triggered > 0:
            direction = SignalDirection.LONG
            strength = (
                min(1.0, signals_triggered / total_indicators) if total_indicators > 0 else 0.5
            )
        elif signals_triggered < 0:
            direction = SignalDirection.SHORT
            strength = (
                min(1.0, abs(signals_triggered) / total_indicators) if total_indicators > 0 else 0.5
            )

        confidence = abs(signals_triggered) / total_indicators if total_indicators > 0 else 0.0

        current_price = closes[-1] if closes else 0.0

        return Signal(
            symbol=symbol,
            exchange=candles.exchange or "kucoin",
            direction=direction,
            strength=strength,
            confidence=confidence,
            price=current_price,
            strategy_id=self.config.name,
        )

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        risk_per_trade: float = 0.02,
    ) -> float:
        """Calculate position size based on risk management."""
        if signal.direction == SignalDirection.NEUTRAL:
            return 0.0

        # Risk-based sizing
        risk_amount = portfolio_value * risk_per_trade

        # Use signal strength for position sizing
        adjusted_risk = risk_amount * signal.strength

        # Calculate quantity based on approximate price
        if signal.price > 0:
            return adjusted_risk / signal.price

        return 0.0
