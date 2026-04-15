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

    def resample(self, target_timeframe: str) -> CandleSeries:
        """Aggregate candles into a higher timeframe.

        Supported target_timeframe values: '1h', '4h', '1d'.
        Source candles must be a lower timeframe aligned to minute boundaries.

        Returns a new CandleSeries with aggregated candles.
        """
        if not self.candles:
            return CandleSeries(
                candles=[],
                symbol=self.symbol,
                exchange=self.exchange,
                timeframe=target_timeframe,
            )

        minutes = {"1h": 60, "4h": 240, "1d": 1440}
        tf_minutes = minutes.get(target_timeframe)
        if tf_minutes is None:
            raise ValueError(f"Unsupported timeframe: {target_timeframe}")

        # Group candles by truncated timestamp
        groups: dict[int, list[Candle]] = {}
        for c in self.candles:
            bucket = int(c.timestamp.timestamp()) // (tf_minutes * 60) * (tf_minutes * 60)
            groups.setdefault(bucket, []).append(c)

        aggregated: list[Candle] = []
        for bucket_ts in sorted(groups):
            group = groups[bucket_ts]
            first = group[0]
            last = group[-1]
            from datetime import datetime as dt, timezone

            aggregated.append(
                Candle(
                    symbol=first.symbol,
                    exchange=first.exchange,
                    timeframe=target_timeframe,
                    timestamp=dt.fromtimestamp(bucket_ts, tz=timezone.utc),
                    open=first.open,
                    high=max(c.high for c in group),
                    low=min(c.low for c in group),
                    close=last.close,
                    volume=sum(c.volume for c in group),
                    quote_volume=sum(c.quote_volume for c in group),
                    trades=sum(c.trades for c in group),
                )
            )

        return CandleSeries(
            candles=aggregated,
            symbol=self.symbol,
            exchange=self.exchange,
            timeframe=target_timeframe,
        )
