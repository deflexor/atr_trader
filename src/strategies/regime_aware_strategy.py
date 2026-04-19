"""Regime-Aware Trading Strategy

Detects market regime (TRENDING vs RANGING) and switches strategy accordingly:
- TRENDING (ADX > 25): Use momentum strategy (trend following)
- RANGING (ADX <= 25): Use mean reversion strategy (fade the move)

Based on "Advances in Financial Machine Learning" regime detection principles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum

from .base_strategy import BaseStrategy, StrategyConfig
from .momentum_strategy import MomentumStrategy, MomentumConfig
from .mean_reversion_strategy import MeanReversionStrategy, MeanReversionConfig
from ..core.models.signal import Signal, SignalDirection
from ..core.models.candle import CandleSeries
from ..core.models.market_data import MarketData


class MarketRegime(Enum):
    """Market regime enumeration."""

    TRENDING = "TRENDING"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


@dataclass
class RegimeAwareConfig(StrategyConfig):
    """Configuration for regime-aware strategy."""

    adx_period: int = 14
    adx_threshold: float = 25.0  # ADX > 25 = trending, <= 25 = ranging
    momentum_config: Optional[MomentumConfig] = None
    mean_reversion_config: Optional[MeanReversionConfig] = None


class RegimeAwareStrategy(BaseStrategy):
    """Strategy that adapts to market regime.

    Detects TRENDING vs RANGING using ADX and switches strategies:
    - TRENDING: Momentum (trend following)
    - RANGING: Mean Reversion (fade the move)

    Composition approach - delegates to underlying strategies.
    """

    def __init__(
        self,
        config: Optional[RegimeAwareConfig] = None,
        momentum_strategy: Optional[MomentumStrategy] = None,
        mean_reversion_strategy: Optional[MeanReversionStrategy] = None,
    ):
        super().__init__(config or RegimeAwareConfig(name="regime_aware"))
        self.regime_config = config or RegimeAwareConfig(name="regime_aware")

        # Use provided strategies or create defaults
        self.momentum = momentum_strategy or MomentumStrategy(
            config=self.regime_config.momentum_config
        )
        self.mean_reversion = mean_reversion_strategy or MeanReversionStrategy(
            config=self.regime_config.mean_reversion_config
        )

        # Cache regime to avoid recalculation on every call
        self._cached_regime = MarketRegime.UNKNOWN
        self._cache_candle_count = 0

    def calculate_adx(self, candles: CandleSeries, period: int = 14) -> float:
        """Calculate Average Directional Index (ADX) for regime detection.

        ADX > 25 indicates TRENDING market
        ADX <= 25 indicates RANGING market

        Uses the standard ADX calculation with Wilder smoothing.
        """
        if len(candles.candles) < period * 2 + 1:
            return 0.0

        n = len(candles.candles)

        # Calculate True Range and Directional Movements
        tr_list = []
        plus_dm_list = []
        minus_dm_list = []

        for i in range(1, n):
            c = candles.candles[i]
            prev_c = candles.candles[i - 1]

            # True Range
            tr = max(
                c.high - c.low,
                abs(c.high - prev_c.close),
                abs(c.low - prev_c.close),
            )
            tr_list.append(tr)

            # +DM and -DM
            high_diff = c.high - prev_c.high
            low_diff = prev_c.low - c.low

            plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
            minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0.0

            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        if len(tr_list) < period:
            return 0.0

        # Smooth using Wilder's method
        smoothed_tr = sum(tr_list[:period])
        smoothed_plus_dm = sum(plus_dm_list[:period])
        smoothed_minus_dm = sum(minus_dm_list[:period])

        # Calculate smoothed +DI, -DI, and DX
        plus_di_list = []
        minus_di_list = []
        dx_list = []

        for i in range(period, len(tr_list)):
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
            smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_list[i]
            smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_list[i]

            if smoothed_tr == 0:
                continue

            plus_di = 100 * (smoothed_plus_dm / smoothed_tr)
            minus_di = 100 * (smoothed_minus_dm / smoothed_tr)

            di_sum = plus_di + minus_di
            if di_sum == 0:
                dx = 0
            else:
                dx = 100 * abs(plus_di - minus_di) / di_sum

            plus_di_list.append(plus_di)
            minus_di_list.append(minus_di)
            dx_list.append(dx)

        if len(dx_list) < period:
            return 0.0

        # ADX is the average of DX values
        adx = sum(dx_list[-period:]) / period
        return adx

    def detect_regime(self, candles: CandleSeries) -> MarketRegime:
        """Detect current market regime using ADX.

        Args:
            candles: Candle series for analysis

        Returns:
            MarketRegime enum (TRENDING or RANGING)
        """
        adx = self.calculate_adx(candles, period=self.regime_config.adx_period)

        if adx > self.regime_config.adx_threshold:
            return MarketRegime.TRENDING
        return MarketRegime.RANGING

    def get_active_strategy(self, candles: CandleSeries) -> BaseStrategy:
        """Get the appropriate strategy for current regime.

        Args:
            candles: Candle series

        Returns:
            Either momentum or mean reversion strategy
        """
        regime = self.detect_regime(candles)

        if regime == MarketRegime.TRENDING:
            return self.momentum
        return self.mean_reversion

    async def generate_signal(
        self,
        symbol: str,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> Signal:
        """Generate regime-aware trading signal.

        Detects regime and delegates to appropriate strategy.
        """
        regime = self.detect_regime(candles)
        active_strategy = self.get_active_strategy(candles)

        signal = await active_strategy.generate_signal(symbol, candles, market_data)

        # Attach regime info for backtesting diagnostics
        signal.strategy_id = f"{signal.strategy_id}_{regime.value.lower()}"

        return signal

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        risk_per_trade: float = 0.02,
    ) -> float:
        """Calculate position size using active strategy's method."""
        # Delegate to momentum (same interface)
        return self.momentum.calculate_position_size(
            signal, portfolio_value, risk_per_trade
        )

    @property
    def diagnostics(self) -> dict:
        """Combined diagnostics from both strategies."""
        combined = self.momentum.diagnostics.copy()
        mr_diags = self.mean_reversion.diagnostics
        for k, v in mr_diags.items():
            if k in combined:
                combined[k] += v
            else:
                combined[k] = v
        return combined


def calculate_adx_pure(candles: list, period: int = 14) -> float:
    """Pure function to calculate ADX from candle data.

    Can be used without a strategy instance.

    Args:
        candles: List of candle dicts with high, low, close
        period: ADX calculation period

    Returns:
        ADX value (0-100)
    """
    if len(candles) < period * 2 + 1:
        return 0.0

    n = len(candles)

    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, n):
        c = candles[i]
        p = candles[i - 1]

        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"]),
        )
        tr_list.append(tr)

        high_diff = c["high"] - p["high"]
        low_diff = p["low"] - c["low"]

        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0.0

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    if len(tr_list) < period:
        return 0.0

    smoothed_tr = sum(tr_list[:period])
    smoothed_plus_dm = sum(plus_dm_list[:period])
    smoothed_minus_dm = sum(minus_dm_list[:period])

    dx_list = []

    for i in range(period, len(tr_list)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_list[i]

        if smoothed_tr == 0:
            continue

        plus_di = 100 * (smoothed_plus_dm / smoothed_tr)
        minus_di = 100 * (smoothed_minus_dm / smoothed_tr)

        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0

        dx_list.append(dx)

    if len(dx_list) < period:
        return 0.0

    adx = sum(dx_list[-period:]) / period
    return adx