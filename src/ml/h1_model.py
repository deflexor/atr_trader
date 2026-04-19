"""1h LSTM model for trend confirmation.

Architecture:
- Same bidirectional 2-layer LSTM as SignalClassifier (hidden_dims=[128, 64, 32])
- 3-class output: DOWN(0), FLAT(1), UP(2)
- Used as confirmation filter only (does not generate entry signals)
- Processes 1h candle data for trend detection

Role in trading system:
- 1m Price Action → Generate signal
        ↓
- 1h LSTM confirms → "1h trend agrees" ✓
        ↓
- Execute Trade on 1m
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple
import logging

import torch
import torch.nn as nn
import numpy as np

from .features import FeatureEngine, FeatureConfig
from .model import (
    SignalClassifier,
    ModelConfig,
    CLASS_DOWN,
    CLASS_FLAT,
    CLASS_UP,
    class_to_direction,
)

logger = logging.getLogger(__name__)


@dataclass
class H1ModelConfig:
    """Configuration for 1h confirmation model.

    Inherits from ModelConfig but with 1h-specific defaults.
    Longer horizon since 1h candles represent larger timeframes.
    """

    num_features: int = 11  # 3 price + 4 technical + 4 volume (no market depth)
    hidden_dims: list[int] = field(default_factory=lambda: [128, 64, 32])
    num_classes: int = 3  # DOWN, FLAT, UP
    dropout: float = 0.3
    learning_rate: float = 0.001
    batch_size: int = 128  # Match existing model config
    epochs: int = 5  # Match existing model config
    threshold_pct: float = 0.01  # Higher threshold for 1h moves (1%)
    # 1h specific
    timeframe: str = "1h"
    horizon: int = 4  # 4 hours ahead for trend confirmation


class H1Model:
    """1h LSTM for trend confirmation.

    Wrapper around SignalClassifier that:
    - Processes 1h candle data
    - Provides trend confirmation (not signal generation)
    - Uses higher threshold for 1h price movements
    """

    def __init__(self, config: Optional[H1ModelConfig] = None):
        self.config = config or H1ModelConfig()
        self.feature_engine = FeatureEngine(
            FeatureConfig(
                window_size=60,  # 60 x 1h = 2.5 days of history
                horizon=self.config.horizon,
                include_technical=True,
                include_volume=True,
                include_market_depth=False,  # 1h typically doesn't have depth data
            )
        )
        self.model = SignalClassifier(
            ModelConfig(
                num_features=self.feature_engine.num_features,
                hidden_dims=self.config.hidden_dims,
                num_classes=self.config.num_classes,
                dropout=self.config.dropout,
                learning_rate=self.config.learning_rate,
                batch_size=self.config.batch_size,
                epochs=self.config.epochs,
                threshold_pct=self.config.threshold_pct,
            )
        )
        self._is_trained = False

    @property
    def is_trained(self) -> bool:
        """Check if model has been trained."""
        return self._is_trained

    def train(
        self,
        train_features: np.ndarray,
        train_labels: np.ndarray,
        val_features: Optional[np.ndarray] = None,
        val_labels: Optional[np.ndarray] = None,
    ) -> dict:
        """Train the 1h confirmation model.

        Args:
            train_features: Training features [num_samples, window_size, num_features]
            train_labels: Training labels [num_samples] with values 0, 1, 2
            val_features: Optional validation features
            val_labels: Optional validation labels

        Returns:
            Training history dict
        """
        history = self.model.train_model(
            train_features,
            train_labels,
            val_features,
            val_labels,
        )
        self._is_trained = True
        logger.info("H1Model training complete")
        return history

    def confirm_trend(
        self,
        candles_1h: list,
        market_data: Optional[object] = None,
    ) -> Tuple[bool, int, float, np.ndarray]:
        """Confirm if 1h trend agrees with potential trade direction.

        Args:
            candles_1h: List of 1h Candle objects
            market_data: Optional market data for additional features

        Returns:
            (trend_agrees, direction, confidence, probabilities)
            trend_agrees: True if 1h trend is clear (not FLAT)
            direction: 2 (UP), 1 (FLAT), 0 (DOWN) from classifier
            confidence: probability of predicted class
            probabilities: [p(DOWN), p(FLAT), p(UP)]
        """
        if not self._is_trained:
            logger.warning("H1Model not trained yet, returning neutral")
            return False, 1, 0.0, np.array([0.33, 0.34, 0.33])

        # Create features from 1h candles
        from ..core.models.candle import CandleSeries

        candle_series = CandleSeries(candles_1h)
        features = self.feature_engine.create_features(candle_series, market_data)

        # Get prediction
        direction, confidence, probs = self.model.predict_class(features)

        # Trend agrees if confidence is high AND direction is not FLAT
        # Lower threshold to 0.4 to allow more signals through
        trend_agrees = direction != CLASS_FLAT and confidence > 0.4

        # Only log first call per session to avoid spam
        if not hasattr(self, '_logged_recently'):
            logger.info(
                f"H1Model confirm_trend: direction={direction} ({['DOWN','FLAT','UP'][direction]}), "
                f"confidence={confidence:.3f}, trend_agrees={trend_agrees}"
            )
            self._logged_recently = True
        elif not getattr(self, '_h1_log_count', 0) % 100:
            logger.debug(
                f"H1Model: dir={direction}, conf={confidence:.3f}, agrees={trend_agrees}"
            )
        self._h1_log_count = getattr(self, '_h1_log_count', 0) + 1

        return trend_agrees, direction, confidence, probs

    def predict(
        self,
        features: np.ndarray,
    ) -> Tuple[int, float, np.ndarray]:
        """Predict trend direction from features.

        Args:
            features: Feature array [window_size, num_features]

        Returns:
            (class_id, confidence, all_probabilities)
        """
        return self.model.predict_class(features)

    def save(self, path: str) -> None:
        """Save model to disk."""
        torch.save(
            {
                "model_state": self.model.state_dict(),
                "optimizer_state": self.model.optimizer.state_dict(),
                "config": self.config,
                "is_trained": self._is_trained,
            },
            path,
        )
        logger.info(f"H1Model saved to {path}")

    def load(self, path: str) -> None:
        """Load model from disk."""
        checkpoint = torch.load(path, weights_only=False)
        self.model.load_state_dict(checkpoint["model_state"])
        self.model.optimizer.load_state_dict(checkpoint["optimizer_state"])
        self.config = checkpoint["config"]
        self._is_trained = checkpoint.get("is_trained", True)
        logger.info(f"H1Model loaded from {path}")

    def get_trend_direction(self, candles_1h: list) -> int:
        """Get trend direction for use in strategy filtering.

        Args:
            candles_1h: List of 1h Candle objects

        Returns:
            2 (LONG) if 1h trend is UP
            0 (SHORT) if 1h trend is DOWN
            1 (NEUTRAL) if 1h trend is FLAT or uncertain
        """
        _, direction, confidence, _ = self.confirm_trend(candles_1h)

        # Downgrade low-confidence predictions to NEUTRAL
        if confidence < 0.4:
            return 1  # NEUTRAL

        return class_to_direction(direction)
