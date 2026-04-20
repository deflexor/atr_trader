"""Holt-Winters Triple Exponential Smoothing for Volatility Forecasting.

Implements triple exponential smoothing for forecasting volatility regimes.
Based on fecon235's Holt-Winters methodology.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import math


@dataclass
class HoltWintersConfig:
    """Configuration for Holt-Winters predictor."""
    alpha: float = 0.3  # Level smoothing
    beta: float = 0.1   # Trend smoothing
    gamma: float = 0.1  # Seasonal smoothing (if used)
    horizon: int = 3    # Periods ahead to forecast


class HoltWintersPredictor:
    """Holt-Winters Triple Exponential Smoothing for forecasting.

    Uses triple exponential smoothing to forecast future values.
    Suitable for volatility which has both level and trend components.

    Formula:
        Level: L_t = α(Y_t - S_{t-s}) + (1-α)(L_{t-1} + T_{t-1})
        Trend: T_t = β(L_t - L_{t-1}) + (1-β)T_{t-1}
        Forecast: F_{t+h} = L_t + h*T_t
    """

    def __init__(self, config: Optional[HoltWintersConfig] = None):
        self.config = config or HoltWintersConfig()
        self.alpha = self.config.alpha
        self.beta = self.config.beta
        self.gamma = self.config.gamma
        self.horizon = self.config.horizon

        self._level: Optional[float] = None
        self._trend: Optional[float] = None
        self._initialized: bool = False
        self._history: list[float] = []

    def fit(self, data: list[float]) -> "HoltWintersPredictor":
        """Initialize and fit the model with historical data.

        Args:
            data: Historical values to fit on

        Returns:
            Self for chaining
        """
        if len(data) < 3:
            raise ValueError("Need at least 3 data points for Holt-Winters")

        self._history = list(data)

        # Initialize using linear regression on first N points
        n_init = min(10, len(data) - 1)
        init_data = data[:n_init + 1]

        # Simple linear regression for initial level and trend
        n = len(init_data)
        x_mean = sum(range(n)) / n
        y_mean = sum(init_data) / n

        num = sum((x - x_mean) * (y - y_mean) for x, y in enumerate(init_data))
        den = sum((x - x_mean) ** 2 for x in range(n))

        if den > 0:
            slope = num / den
            intercept = y_mean - slope * x_mean
            self._level = intercept + slope * (n - 1)
            self._trend = slope
        else:
            self._level = y_mean
            self._trend = 0

        self._initialized = True
        return self

    def update(self, value: float) -> None:
        """Update the model with a new observation.

        Args:
            value: New observation
        """
        if not self._initialized:
            self._level = value
            self._trend = 0
            self._initialized = True
            self._history.append(value)
            return

        # Update level
        last_level = self._level
        self._level = self.alpha * value + (1 - self.alpha) * (self._level + self._trend)

        # Update trend
        self._trend = self.beta * (self._level - last_level) + (1 - self.beta) * self._trend

        self._history.append(value)

    def forecast(self, horizon: Optional[int] = None) -> list[float]:
        """Forecast future values.

        Args:
            horizon: Number of periods ahead to forecast (default: self.horizon)

        Returns:
            List of forecasted values
        """
        if not self._initialized:
            raise ValueError("Model not initialized. Call fit() first.")

        if horizon is None:
            horizon = self.horizon

        forecasts = []
        for h in range(1, horizon + 1):
            forecast = self._level + h * self._trend
            forecasts.append(max(0, forecast))  # Volatility can't be negative

        return forecasts

    def predict_next(self) -> float:
        """Predict the next value (1-step ahead forecast).

        Returns:
            Predicted next value
        """
        forecasts = self.forecast(1)
        return forecasts[0] if forecasts else 0.0

    def volatility_ratio(self) -> float:
        """Get ratio of predicted volatility to current level.

        Returns:
            ratio = forecast[0] / current_level
            > 1 means volatility expected to increase
            < 1 means volatility expected to decrease
        """
        if not self._initialized or len(self._history) < 2:
            return 1.0

        current = self._history[-1] if self._history else 0
        predicted = self.predict_next()

        if current <= 0:
            return 1.0

        return predicted / current

    @property
    def level(self) -> Optional[float]:
        """Current smoothed level."""
        return self._level

    @property
    def trend(self) -> Optional[float]:
        """Current smoothed trend."""
        return self._trend

    @property
    def history(self) -> list[float]:
        """Copy of historical data."""
        return list(self._history)


def calculate_exponential_moving_average(
    data: list[float],
    period: int,
    smoothing: Optional[float] = None
) -> list[float]:
    """Calculate Exponential Moving Average.

    Args:
        data: Price or indicator values
        period: EMA period
        smoothing: Smoothing factor (default 2/(period+1))

    Returns:
        EMA values
    """
    if smoothing is None:
        smoothing = 2.0 / (period + 1)

    if len(data) < period:
        return []

    ema = [sum(data[:period]) / period]  # First EMA is SMA

    for i in range(period, len(data)):
        ema.append((data[i] - ema[-1]) * smoothing + ema[-1])

    return ema


def calculate_double_ema(data: list[float], period: int) -> list[float]:
    """Calculate Double EMA (DEMA) - faster response than EMA.

    Args:
        data: Price or indicator values
        period: DEMA period

    Returns:
        DEMA values
    """
    ema1 = calculate_exponential_moving_average(data, period)
    ema2 = calculate_exponential_moving_average(ema1, period)

    # DEMA = 2*EMA1 - EMA2
    return [2 * e1 - e2 for e1, e2 in zip(ema1, ema2)]


def calculate_triple_ema(data: list[float], period: int) -> list[float]:
    """Calculate Triple EMA (TEMA) - even faster response.

    Args:
        data: Price or indicator values
        period: TEMA period

    Returns:
        TEMA values
    """
    ema1 = calculate_exponential_moving_average(data, period)
    ema2 = calculate_exponential_moving_average(ema1, period)
    ema3 = calculate_exponential_moving_average(ema2, period)

    # TEMA = 3*EMA1 - 3*EMA2 + EMA3
    return [3 * e1 - 3 * e2 + e3 for e1, e2, e3 in zip(ema1, ema2, ema3)]