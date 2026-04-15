"""Candle/OHLCV model for historical price data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Candle:
    """Immutable OHLCV candle data.

    Represents a single time period of price action with Open,
    High, Low, Close, and Volume.

    """

    symbol: str
    exchange: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0  # volume in quote currency
    trades: int = 0  # number of trades

    @property
    def typical_price(self) -> float:
        """Calculate typical price (HLC average)."""
        return (self.high + self.low + self.close) / 3

    @property
    def typical_price_change(self) -> float:
        """Calculate price change from open to typical."""
        return self.typical_price - self.open

    @property
    def range(self) -> float:
        """Calculate high-low range."""
        return self.high - self.low

    @property
    def body_size(self) -> float:
        """Calculate candle body size."""
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        """Check if candle is bullish (close > open)."""
        return self.close > self.open

    @property
    def is_doji(self) -> bool:
        """Check if candle is a doji (small body)."""
        return self.body_size < self.range * 0.1

    def with_indicators(self, indicators: dict) -> Candle:
        """Create new candle with added indicators (immutable)."""
        return Candle(
            symbol=self.symbol,
            exchange=self.exchange,
            timeframe=self.timeframe,
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            quote_volume=self.quote_volume,
            trades=self.trades,
        )


@dataclass
class CandleSeries:
    """Container for a series of candles with analysis helpers."""

    candles: list[Candle] = field(default_factory=list)
    symbol: str = ""
    exchange: str = ""
    timeframe: str = ""

    @property
    def closes(self) -> list[float]:
        """Get list of closing prices."""
        return [c.close for c in self.candles]

    @property
    def highs(self) -> list[float]:
        """Get list of high prices."""
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        """Get list of low prices."""
        return [c.low for c in self.candles]

    @property
    def volumes(self) -> list[float]:
        """Get list of volumes."""
        return [c.volume for c in self.candles]

    def latest(self, n: int = 1) -> list[Candle]:
        """Get the n most recent candles."""
        return self.candles[-n:] if self.candles else []

    def add(self, candle: Candle) -> None:
        """Add a candle to the series."""
        self.candles.append(candle)
