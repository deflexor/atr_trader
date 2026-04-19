"""1h data pipeline for LSTM training.

Fetches, stores, and prepares 1-hour candle data for LSTM model training.
Designed to integrate with existing FeatureEngine and KuCoin adapter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any

import numpy as np

from ..adapters.kucoin_adapter import KuCoinAdapter
from ..core.db.datastore import DataStore
from ..core.models.candle import Candle, CandleSeries
from .features import FeatureEngine, FeatureConfig

logger = logging.getLogger(__name__)


@dataclass
class H1PipelineConfig:
    """Configuration for 1h data pipeline."""

    symbol: str = "BTCUSDT"
    exchange: str = "kucoin"
    timeframe: str = "1h"
    lookback_days: int = 90  # Historical data lookback
    min_candles: int = 1000  # Minimum candles for training
    feature_config: FeatureConfig = field(default_factory=FeatureConfig)
    datastore_dir: str = "data"


class H1DataPipeline:
    """1-hour data pipeline for LSTM training.

    Handles:
    - Fetching 1h candles from KuCoin
    - Storing/retrieving from SQLite via DataStore
    - Preparing features using existing FeatureEngine
    - Data validation and quality checks
    """

    def __init__(
        self,
        adapter: Optional[KuCoinAdapter] = None,
        datastore: Optional[DataStore] = None,
        config: Optional[H1PipelineConfig] = None,
    ):
        self.config = config or H1PipelineConfig()
        self.adapter = adapter or KuCoinAdapter()
        self.datastore = datastore or DataStore(self.config.datastore_dir)
        # 1h model doesn't use market depth (no live order book at 1h)
        feature_config = FeatureConfig(
            window_size=self.config.feature_config.window_size,
            horizon=self.config.feature_config.horizon,
            include_technical=True,
            include_volume=True,
            include_market_depth=False,
        )
        self.feature_engine = FeatureEngine(feature_config)

    def _to_kucoin_timeframe(self, timeframe: str) -> str:
        """Convert standard timeframe to KuCoin format."""
        mapping = {
            "1m": "1min",
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "1h": "1hour",
            "4h": "4hour",
            "1d": "1day",
            "1w": "1week",
        }
        return mapping.get(timeframe, timeframe)

    async def fetch_and_store(self) -> int:
        """Fetch 1h candles from exchange and store in database.

        Returns:
            Number of new candles stored
        """
        logger.info(f"Fetching {self.config.lookback_days} days of {self.config.timeframe} candles")

        kucoin_tf = self._to_kucoin_timeframe(self.config.timeframe)
        raw_candles = await self.adapter.fetch_historical_by_period(
            symbol=self.config.symbol,
            timeframe=kucoin_tf,
            days=self.config.lookback_days,
        )

        if not raw_candles:
            logger.warning("No candles fetched from exchange")
            return 0

        # Convert raw KuCoin format to Candle objects
        candles = self._parse_candles(raw_candles)
        logger.info(f"Parsed {len(candles)} raw candles")

        # Deduplicate by timestamp
        candles = self._deduplicate(candles)

        # Store in database
        inserted = self.datastore.save_candles(candles)
        logger.info(f"Stored {inserted} new candles")

        return inserted

    def _parse_candles(self, raw_candles: list[dict]) -> list[Candle]:
        """Parse raw KuCoin candle data into Candle objects.

        KuCoin returns: [timestamp, open, close, high, low, volume, turnover]
        """
        candles = []
        for raw in raw_candles:
            try:
                ts = int(raw[0])
                candles.append(
                    Candle(
                        symbol=self.config.symbol,
                        exchange=self.config.exchange,
                        timeframe=self.config.timeframe,
                        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                        open=float(raw[1]),
                        close=float(raw[2]),
                        high=float(raw[3]),
                        low=float(raw[4]),
                        volume=float(raw[5]),
                        quote_volume=float(raw[6]) if len(raw) > 6 else 0.0,
                    )
                )
            except (ValueError, IndexError) as e:
                logger.debug(f"Skipping malformed candle: {e}")
                continue

        return candles

    def _deduplicate(self, candles: list[Candle]) -> list[Candle]:
        """Remove duplicate candles by timestamp, keeping oldest."""
        seen: dict[int, Candle] = {}
        for c in candles:
            ts = int(c.timestamp.timestamp())
            if ts not in seen:
                seen[ts] = c

        result = list(seen.values())
        result.sort(key=lambda x: x.timestamp.timestamp())
        return result

    def load_from_db(self, limit: Optional[int] = None) -> CandleSeries:
        """Load 1h candles from database.

        Args:
            limit: Maximum number of candles to load

        Returns:
            CandleSeries with 1h candles
        """
        candles = self.datastore.get_candles(
            symbol=self.config.symbol,
            exchange=self.config.exchange,
            timeframe=self.config.timeframe,
            limit=limit,
        )

        logger.info(f"Loaded {len(candles)} candles from database")

        return CandleSeries(
            candles=candles,
            symbol=self.config.symbol,
            exchange=self.config.exchange,
            timeframe=self.config.timeframe,
        )

    def prepare_data(
        self,
        candles: CandleSeries,
        include_market_depth: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Prepare features and labels from 1h candles.

        Args:
            candles: CandleSeries with 1h data
            include_market_depth: Whether to include market depth features

        Returns:
            (features, labels) tuple for LSTM training
        """
        if len(candles.candles) < self.config.feature_config.window_size:
            raise ValueError(
                f"Insufficient candles: {len(candles.candles)} < "
                f"{self.config.feature_config.window_size}"
            )

        config = self.config.feature_config
        lookback = config.window_size
        horizon = config.horizon

        features_list = []
        labels_list = []

        max_idx = len(candles.candles) - horizon - 1

        for i in range(max_idx - lookback + 1):
            window = candles.candles[i : i + lookback]
            window_series = CandleSeries(
                candles=window,
                symbol=candles.symbol,
                exchange=candles.exchange,
                timeframe=candles.timeframe,
            )

            # Create features
            features = self.feature_engine.create_features(window_series)
            features_list.append(features)

            # Create label: future return over horizon
            current_close = window[-1].close
            future_close = candles.candles[i + lookback + horizon - 1].close
            label = (future_close - current_close) / current_close
            labels_list.append(label)

            # Rate limit to avoid flooding
            if i % 500 == 0:
                time.sleep(0.01)

        features = np.array(features_list)
        labels = np.array(labels_list)

        # Normalize labels to [0, 1] range for signal strength
        labels_min = labels.min()
        labels_max = labels.max()
        if labels_max - labels_min > 1e-6:
            labels = (labels - labels_min) / (labels_max - labels_min)
        else:
            labels = np.full_like(labels, 0.5)

        logger.info(f"Prepared {len(features)} samples, label range: [{labels_min:.4f}, {labels_max:.4f}]")

        return features, labels

    def validate_data(self, candles: CandleSeries) -> dict[str, Any]:
        """Validate 1h candle data quality.

        Args:
            candles: CandleSeries to validate

        Returns:
            Validation report with issues found
        """
        issues = []
        stats = {
            "total_candles": len(candles.candles),
            "min_price": None,
            "max_price": None,
            "gaps": 0,
            "duplicates": 0,
        }

        if not candles.candles:
            issues.append("No candles in series")
            return {"valid": False, "issues": issues, "stats": stats}

        # Price range check
        closes = candles.closes
        if closes:
            stats["min_price"] = min(closes)
            stats["max_price"] = max(closes)

            # Check for extreme jumps
            for i in range(1, len(closes)):
                ratio = closes[i] / closes[i - 1] if closes[i - 1] > 0 else 1
                if ratio > 2 or ratio < 0.5:
                    issues.append(f"Extreme price jump at index {i}: {ratio:.2f}x")

        # Gap detection (expected 1h intervals)
        expected_interval = 3600  # seconds
        for i in range(1, len(candles.candles)):
            diff = candles.candles[i].timestamp.timestamp() - candles.candles[i - 1].timestamp.timestamp()
            if abs(diff - expected_interval) > 60:  # Allow 1 min tolerance
                stats["gaps"] += 1

        # Duplicate check
        timestamps = [c.timestamp for c in candles.candles]
        unique_ts = set()
        for ts in timestamps:
            s = int(ts.timestamp())
            if s in unique_ts:
                stats["duplicates"] += 1
            unique_ts.add(s)

        # Minimum candles check
        if stats["total_candles"] < self.config.min_candles:
            issues.append(
                f"Insufficient candles: {stats['total_candles']} < {self.config.min_candles}"
            )

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "stats": stats,
        }

    def get_latest_timestamp(self) -> Optional[datetime]:
        """Get timestamp of most recent candle in database."""
        candles = self.load_from_db(limit=1)
        return candles.candles[-1].timestamp if candles.candles else None

    def needs_update(self, max_age_hours: int = 2) -> bool:
        """Check if data needs to be refreshed.

        Args:
            max_age_hours: Maximum acceptable age before update needed

        Returns:
            True if update needed
        """
        latest = self.get_latest_timestamp()
        if not latest:
            return True

        age_seconds = (datetime.now(timezone.utc) - latest).total_seconds()
        return age_seconds > (max_age_hours * 3600)