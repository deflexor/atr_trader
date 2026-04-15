"""Training pipeline for neural network signal prediction.

Handles data loading, training, validation, and model persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
import logging

import numpy as np
import torch

from .features import FeatureEngine, FeatureConfig
from .model import SignalPredictor, ModelConfig, EnsemblePredictor

logger = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    """Training pipeline configuration."""

    feature_config: FeatureConfig = field(default_factory=FeatureConfig)
    model_config: ModelConfig = field(default_factory=ModelConfig)
    train_split: float = 0.8
    val_split: float = 0.1
    test_split: float = 0.1
    early_stopping_patience: int = 10
    model_save_path: str = "models/signal_predictor.pt"
    seed: int = 42


class TrainingPipeline:
    """Training pipeline for signal prediction model.

    Handles:
    - Data loading and preprocessing
    - Feature engineering
    - Train/val/test split
    - Model training with early stopping
    - Model evaluation and persistence
    """

    def __init__(self, config: Optional[TrainingConfig] = None):
        self.config = config or TrainingConfig()
        self.feature_engine = FeatureEngine(self.config.feature_config)
        self.model: Optional[SignalPredictor] = None
        self.history: Optional[dict] = None

        # Set random seed
        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)

    def load_candles(self, path: str) -> np.ndarray:
        """Load historical candles from file.

        Args:
            path: Path to candle data file (CSV or numpy format)

        Returns:
            Array of candle data

        """
        path = Path(path)

        if path.suffix == ".npy":
            return np.load(path)
        elif path.suffix == ".csv":
            return np.genfromtxt(path, delimiter=",", skip_header=1)
        else:
            raise ValueError(f"Unsupported file format: {path.suffix}")

    def prepare_data(
        self,
        candles: list,
        train_split: Optional[float] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Prepare features and labels from candle data.

        Args:
            candles: List of Candle objects or candle data
            train_split: Override train split ratio

        Returns:
            (train_features, train_labels, val_features, val_labels, test_features, test_labels)

        """
        split = train_split or self.config.train_split

        # Create feature matrix
        features = []
        labels = []

        for i in range(
            len(candles)
            - self.config.feature_config.window_size
            - self.config.feature_config.horizon
        ):
            window = candles[i : i + self.config.feature_config.window_size]
            horizon_labels = candles[
                i + self.config.feature_config.window_size : i
                + self.config.feature_config.window_size
                + self.config.feature_config.horizon
            ]

            # Create features from window
            feature_matrix = self.feature_engine.create_features(window)
            features.append(feature_matrix)

            # Calculate label (future return)
            if hasattr(window[-1], "close") and hasattr(horizon_labels[-1], "close"):
                label = (horizon_labels[-1].close - window[-1].close) / window[-1].close
            else:
                label = 0

            labels.append(label)

        features = np.array(features)
        labels = np.array(labels)

        # Normalize labels to 0-1 range (signal strength)
        labels_min = labels.min()
        labels_max = labels.max()
        if labels_max - labels_min > 0:
            labels = (labels - labels_min) / (labels_max - labels_min)
        else:
            labels = np.ones_like(labels) * 0.5

        # Train/val/test split
        n = len(features)
        train_end = int(n * split)
        val_end = int(n * (split + self.config.val_split))

        train_features = features[:train_end]
        train_labels = labels[:train_end]

        val_features = features[train_end:val_end]
        val_labels = labels[train_end:val_end]

        test_features = features[val_end:]
        test_labels = labels[val_end:]

        logger.info(
            f"Data split: train={len(train_features)}, val={len(val_features)}, test={len(test_features)}"
        )

        return train_features, train_labels, val_features, val_labels, test_features, test_labels

    def train(
        self,
        train_features: np.ndarray,
        train_labels: np.ndarray,
        val_features: Optional[np.ndarray] = None,
        val_labels: Optional[np.ndarray] = None,
    ) -> dict:
        """Train the signal prediction model.

        Args:
            train_features: Training features
            train_labels: Training labels
            val_features: Optional validation features
            val_labels: Optional validation labels

        Returns:
            Training history

        """
        if self.model is None:
            self.model = SignalPredictor(self.config.model_config)

        self.history = self.model.train_model(
            train_features,
            train_labels,
            val_features,
            val_labels,
        )

        return self.history

    def evaluate(
        self,
        test_features: np.ndarray,
        test_labels: np.ndarray,
    ) -> dict:
        """Evaluate model on test set.

        Args:
            test_features: Test features
            test_labels: Test labels

        Returns:
            Evaluation metrics

        """
        if self.model is None:
            raise RuntimeError("Model not trained yet")

        self.model.eval()
        with torch.no_grad():
            X = torch.FloatTensor(test_features)
            y = torch.FloatTensor(test_labels).unsqueeze(1)
            predictions = self.model(X).squeeze()

            # Calculate metrics
            mse = torch.mean((predictions - torch.FloatTensor(test_labels)) ** 2).item()
            mae = torch.mean(torch.abs(predictions - torch.FloatTensor(test_labels))).item()

            # Direction accuracy (predicted direction vs actual)
            pred_direction = (predictions > 0.5).float()
            actual_direction = (torch.FloatTensor(test_labels) > 0.5).float()
            direction_acc = (pred_direction == actual_direction).float().mean().item()

        return {
            "mse": mse,
            "mae": mae,
            "direction_accuracy": direction_acc,
        }

    def save_model(self, path: Optional[str] = None) -> None:
        """Save trained model."""
        if self.model is None:
            raise RuntimeError("No model to save")

        save_path = path or self.config.model_save_path
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        self.model.save(save_path)
        logger.info(f"Model saved to {save_path}")

    def load_model(self, path: str) -> None:
        """Load trained model."""
        if self.model is None:
            self.model = SignalPredictor(self.config.model_config)
        self.model.load(path)
        logger.info(f"Model loaded from {path}")

    def predict(self, features: np.ndarray) -> float:
        """Predict signal strength from features.

        Args:
            features: Feature matrix [window_size, num_features]

        Returns:
            Signal strength prediction (0-1)

        """
        if self.model is None:
            raise RuntimeError("Model not loaded or trained")

        return self.model.predict(features)

    def run(
        self,
        candles_path: str,
        save_model: bool = True,
    ) -> dict:
        """Run full training pipeline.

        Args:
            candles_path: Path to historical candle data
            save_model: Whether to save the trained model

        Returns:
            Training results and evaluation metrics

        """
        logger.info(f"Loading candles from {candles_path}")
        candles = self.load_candles(candles_path)

        logger.info("Preparing data...")
        train_feat, train_labels, val_feat, val_labels, test_feat, test_labels = self.prepare_data(
            candles
        )

        logger.info("Training model...")
        self.train(train_feat, train_labels, val_feat, val_labels)

        logger.info("Evaluating on test set...")
        metrics = self.evaluate(test_feat, test_labels)

        if save_model:
            self.save_model()

        return {
            "history": self.history,
            "metrics": metrics,
            "num_train_samples": len(train_feat),
            "num_test_samples": len(test_feat),
        }
