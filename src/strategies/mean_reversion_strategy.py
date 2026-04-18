"""Mean reversion trading strategy.

Enters positions when price deviates from moving average.
Expects price to return to average (mean reversion).
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
class MeanReversionConfig(StrategyConfig):
    """Configuration for mean reversion strategy."""

    sma_period: int = 20
    deviation_threshold: float = 0.025  # 2.5% deviation from SMA (balanced)
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 35.0  # Standard RSI oversold level
    rsi_overbought: float = 65.0  # Standard RSI overbought level


class MeanReversionStrategy(BaseStrategy):
    """Mean reversion trading strategy.

    Generates signals when price deviates significantly from its moving average,
    expecting the price to revert to the mean.

    Uses:
    - Simple Moving Average (SMA) as the mean
    - Bollinger Bands for deviation measurement
    - RSI to confirm overbought/oversold conditions
    """

    def __init__(self, config: Optional[MeanReversionConfig] = None):
        super().__init__(config or MeanReversionConfig(name="mean_reversion"))
        self.mr_config = config or MeanReversionConfig(name="mean_reversion")

    def calculate_sma(self, prices: list[float], period: int) -> Optional[float]:
        """Calculate Simple Moving Average."""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    def calculate_bollinger_bands(
        self,
        prices: list[float],
        period: int = 20,
        std_multiplier: float = 2.0,
    ) -> Optional[tuple[float, float, float]]:
        """Calculate Bollinger Bands (upper, middle, lower)."""
        if len(prices) < period:
            return None

        sma = self.calculate_sma(prices, period)
        if sma is None:
            return None

        variance = sum((p - sma) ** 2 for p in prices[-period:]) / period
        std_dev = variance**0.5

        upper = sma + (std_multiplier * std_dev)
        lower = sma - (std_multiplier * std_dev)

        return upper, sma, lower

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

    async def generate_signal(
        self,
        symbol: str,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> Signal:
        """Generate mean reversion trading signal."""
        if len(candles.candles) < self.mr_config.bollinger_period + 1:
            return Signal(
                symbol=symbol,
                exchange=candles.exchange or "kucoin",
                direction=SignalDirection.NEUTRAL,
                strength=0.0,
                confidence=0.0,
                strategy_id=self.config.name,
            )

        closes = candles.closes
        current_price = closes[-1]

        # Calculate indicators
        sma = self.calculate_sma(closes, self.mr_config.sma_period)
        bollinger = self.calculate_bollinger_bands(
            closes,
            self.mr_config.bollinger_period,
            self.mr_config.bollinger_std,
        )
        rsi = self.calculate_rsi(closes, self.mr_config.rsi_period)

        if sma is None or bollinger is None:
            return Signal(
                symbol=symbol,
                exchange=candles.exchange or "kucoin",
                direction=SignalDirection.NEUTRAL,
                strength=0.0,
                confidence=0.0,
                strategy_id=self.config.name,
            )

        upper, middle, lower = bollinger

        # Calculate deviation from SMA
        deviation = (current_price - sma) / sma

        # Determine direction and strength
        direction = SignalDirection.NEUTRAL
        strength = 0.0
        confidence = 0.0

        signals_triggered = 0
        total_indicators = 0

        # Bollinger Band analysis
        total_indicators += 1
        if current_price < lower:
            # Price below lower band - potential buy (reversion up)
            band_deviation = (lower - current_price) / lower
            signals_triggered += 1
        elif current_price > upper:
            # Price above upper band - potential sell (reversion down)
            band_deviation = (current_price - upper) / upper
            signals_triggered -= 1

        # Deviation threshold analysis
        total_indicators += 1
        if abs(deviation) > self.mr_config.deviation_threshold:
            if deviation < 0:
                signals_triggered += 1  # Below average, expect bounce up
            else:
                signals_triggered -= 1  # Above average, expect drop down

        # RSI confirmation
        if rsi is not None:
            total_indicators += 1
            if rsi < self.mr_config.rsi_oversold:
                signals_triggered += 1
            elif rsi > self.mr_config.rsi_overbought:
                signals_triggered -= 1

        # Calculate final direction and strength
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

        risk_amount = portfolio_value * risk_per_trade
        adjusted_risk = risk_amount * signal.strength

        if signal.price > 0:
            return adjusted_risk / signal.price

        return 0.0
