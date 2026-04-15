"""Feature engineering for neural net signal prediction.

Creates features from market data for ML model training.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

import numpy as np

from ..core.models.candle import Candle, CandleSeries
from ..core.models.market_data import MarketData


@dataclass
class FeatureConfig:
    """Configuration for feature engineering."""

    window_size: int = 60  # Lookback window for features
    horizon: int = 5  # Prediction horizon (steps ahead)
    include_technical: bool = True
    include_volume: bool = True
    include_market_depth: bool = True


class FeatureEngine:
    """Feature engineering for trading signals.

    Creates features from raw market data for neural network training.
    Features include:
    - Price returns and volatility
    - Technical indicators (RSI, MACD, Bollinger)
    - Volume features
    - Order flow indicators
    """

    def __init__(self, config: Optional[FeatureConfig] = None):
        self.config = config or FeatureConfig()
        self.feature_names: list[str] = []

    @property
    def num_features(self) -> int:
        """Calculate total number of features based on default config (without market_data).

        This is used for model configuration when market_data is not provided.
        """
        num = 3  # Returns, normalized price, HL range
        if self.config.include_technical:
            num += 4  # RSI, MACD, BB, MA diff
        if self.config.include_volume:
            num += 3  # Volume norm, momentum, VWAP
        # Market depth features only when market_data is provided
        return num

    def create_features(
        self,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> np.ndarray:
        """Create feature matrix from market data.

        Args:
            candles: Historical candle data
            market_data: Current market snapshot (optional)

        Returns:
            numpy array of features [window_size, num_features]

        """
        if len(candles.candles) < self.config.window_size:
            return np.zeros((self.config.window_size, self._num_features))

        features = []

        # Price-based features
        features.extend(self._price_features(candles))

        # Technical indicators
        if self.config.include_technical:
            features.extend(self._technical_features(candles))

        # Volume features
        if self.config.include_volume:
            features.extend(self._volume_features(candles))

        # Market depth features (if available)
        if self.config.include_market_depth and market_data:
            features.extend(self._market_depth_features(market_data))

        # Transpose to [window_size, num_features]
        return np.array(features).T

    def create_labels(
        self,
        candles: CandleSeries,
        horizon: Optional[int] = None,
    ) -> np.ndarray:
        """Create labels for supervised learning.

        Labels are future returns over the horizon period.

        Args:
            candles: Historical candle data
            horizon: Prediction horizon (steps ahead)

        Returns:
            numpy array of labels [window_size]

        """
        h = horizon or self.config.horizon
        closes = np.array(candles.closes)

        if len(closes) < self.config.window_size + h:
            return np.zeros(self.config.window_size)

        # Calculate future returns
        returns = []
        for i in range(self.config.window_size, len(closes)):
            future_return = (closes[i + h] - closes[i]) / closes[i] if i + h < len(closes) else 0
            returns.append(future_return)

        return np.array(returns)

    def _price_features(self, candles: CandleSeries) -> list[list[float]]:
        """Create price-based features."""
        closes = np.array(candles.closes)
        highs = np.array(candles.highs)
        lows = np.array(candles.lows)

        features = []

        # Returns
        returns = np.diff(closes) / closes[:-1]
        returns = np.concatenate([[0], returns])

        # Normalized prices
        normalized = closes / closes[0] - 1 if closes[0] != 0 else np.zeros_like(closes)

        # High-Low range normalized
        hl_range = (highs - lows) / closes
        hl_range = np.concatenate([[0], hl_range[:-1]])

        features.append(returns[-self.config.window_size :].tolist())
        features.append(normalized[-self.config.window_size :].tolist())
        features.append(hl_range[-self.config.window_size :].tolist())

        return features

    def _technical_features(self, candles: CandleSeries) -> list[list[float]]:
        """Create technical indicator features."""
        closes = np.array(candles.closes)
        features = []

        # RSI
        rsi = self._calculate_rsi(closes, 14)
        features.append(
            rsi[-self.config.window_size :].tolist()
            if rsi is not None
            else [0] * self.config.window_size
        )

        # MACD
        macd = self._calculate_macd(closes)
        if macd is not None:
            features.append(macd[-self.config.window_size :].tolist())
        else:
            features.append([0] * self.config.window_size)

        # Bollinger Bands position
        bb_pos = self._calculate_bb_position(closes, 20, 2)
        features.append(
            bb_pos[-self.config.window_size :].tolist()
            if bb_pos is not None
            else [0] * self.config.window_size
        )

        # Moving average crossover
        ma_diff = self._calculate_ma_diff(closes, 10, 30)
        features.append(
            ma_diff[-self.config.window_size :].tolist()
            if ma_diff is not None
            else [0] * self.config.window_size
        )

        return features

    def _volume_features(self, candles: CandleSeries) -> list[list[float]]:
        """Create volume-based features."""
        volumes = np.array(candles.volumes)
        features = []

        # Volume normalized
        avg_volume = np.mean(volumes[-self.config.window_size :])
        vol_norm = volumes / avg_volume - 1 if avg_volume > 0 else np.zeros_like(volumes)
        features.append(vol_norm[-self.config.window_size :].tolist())

        # Volume momentum
        vol_ma = np.convolve(volumes, np.ones(5) / 5, mode="same")
        vol_momentum = np.diff(vol_ma) / (vol_ma[:-1] + 1e-10)
        vol_momentum = np.concatenate([[0], vol_momentum])
        features.append(vol_momentum[-self.config.window_size :].tolist())

        # VWAP deviation
        vwap = self._calculate_vwap(candles)
        if vwap is not None:
            closes = np.array(candles.closes)
            vwap_dev = (closes - vwap) / vwap
            features.append(vwap_dev[-self.config.window_size :].tolist())
        else:
            features.append([0] * self.config.window_size)

        return features

    def _market_depth_features(self, market_data: MarketData) -> list[float]:
        """Create market depth features from current market data."""
        features = []

        # Spread
        features.append(market_data.spread)

        # Spread as percentage
        features.append(market_data.spread_pct)

        # Bid-ask size ratio
        if market_data.ask_size > 0:
            features.append(market_data.bid_size / market_data.ask_size)
        else:
            features.append(1.0)

        # Mid price momentum (if we have history)
        if market_data.last_price and market_data.last_price > 0:
            features.append(0)  # Placeholder for mid price momentum
        else:
            features.append(0)

        return features

    @property
    def _num_features(self) -> int:
        """Calculate total number of features."""
        num = 3  # Returns, normalized price, HL range
        if self.config.include_technical:
            num += 4  # RSI, MACD, BB, MA diff
        if self.config.include_volume:
            num += 3  # Volume norm, momentum, VWAP
        if self.config.include_market_depth:
            num += 4  # Spread, spread%, bid/ask ratio, momentum
        return num

    def _calculate_rsi(self, prices: np.ndarray, period: int = 14) -> np.ndarray:
        """Calculate RSI indicator."""
        if len(prices) < period + 1:
            return np.zeros(len(prices))

        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gains = np.convolve(gains, np.ones(period) / period, mode="same")
        avg_losses = np.convolve(losses, np.ones(period) / period, mode="same")

        rs = avg_gains / (avg_losses + 1e-10)
        rsi = 100 - (100 / (1 + rs))

        return np.concatenate([[50], rsi])  # First value neutral

    def _calculate_macd(
        self,
        prices: np.ndarray,
        fast: int = 12,
        slow: int = 26,
    ) -> np.ndarray | None:
        """Calculate MACD histogram."""
        if len(prices) < slow:
            return None

        def ema(data: np.ndarray, period: int) -> np.ndarray:
            multiplier = 2 / (period + 1)
            result = np.zeros(len(data))
            result[0] = data[0]
            for i in range(1, len(data)):
                result[i] = data[i] * multiplier + result[i - 1] * (1 - multiplier)
            return result

        ema_fast = ema(prices, fast)
        ema_slow = ema(prices, slow)

        macd = ema_fast - ema_slow
        signal = ema(macd, 9)
        histogram = macd - signal

        return histogram

    def _calculate_bb_position(
        self,
        prices: np.ndarray,
        period: int = 20,
        std_mult: float = 2.0,
    ) -> np.ndarray | None:
        """Calculate Bollinger Band position (0-1)."""
        if len(prices) < period:
            return None

        sma = np.convolve(prices, np.ones(period) / period, mode="valid")
        std = np.array([np.std(prices[i : i + period]) for i in range(len(prices) - period + 1)])

        upper = sma + std_mult * std
        lower = sma - std_mult * std

        bb_pos = (prices[period - 1 :] - lower) / (upper - lower + 1e-10)

        # Pad to original length
        return np.concatenate([np.zeros(period - 1), bb_pos])

    def _calculate_ma_diff(self, prices: np.ndarray, fast: int, slow: int) -> np.ndarray | None:
        """Calculate moving average difference."""
        if len(prices) < slow:
            return None

        ma_fast = np.convolve(prices, np.ones(fast) / fast, mode="valid")
        ma_slow = np.convolve(prices, np.ones(slow) / slow, mode="valid")

        # Align arrays by using the shorter length (they overlap only in the valid region)
        min_len = min(len(ma_fast), len(ma_slow))
        diff = ma_fast[-min_len:] - ma_slow[-min_len:]

        # Normalize by slow MA
        diff = diff / ma_slow[-min_len:]

        # Pad to original length
        return np.concatenate([np.zeros(slow - 1), diff])

    def _calculate_vwap(self, candles: CandleSeries) -> np.ndarray | None:
        """Calculate Volume Weighted Average Price."""
        if not candles.candles:
            return None

        typical_prices = np.array([c.typical_price for c in candles.candles])
        volumes = np.array([c.volume for c in candles.candles])

        cumulative_tp_volume = np.cumsum(typical_prices * volumes)
        cumulative_volume = np.cumsum(volumes)

        vwap = cumulative_tp_volume / (cumulative_volume + 1e-10)
        return vwap

    def get_feature_names(self) -> list[str]:
        """Get list of feature names for model interpretation."""
        if self.feature_names:
            return self.feature_names

        names = ["returns", "normalized_price", "hl_range"]
        if self.config.include_technical:
            names.extend(["rsi", "macd_hist", "bb_position", "ma_diff"])
        if self.config.include_volume:
            names.extend(["vol_normalized", "vol_momentum", "vwap_dev"])
        if self.config.include_market_depth:
            names.extend(["spread", "spread_pct", "bid_ask_ratio", "mid_momentum"])

        self.feature_names = names
        return names
