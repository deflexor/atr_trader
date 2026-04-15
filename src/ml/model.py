"""Neural network model for signal strength prediction.

Predicts signal strength based on engineered features.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
import logging

import torch
import torch.nn as nn
import numpy as np

from .features import FeatureEngine, FeatureConfig

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    """Neural network configuration."""

    input_size: int = 60  # window_size
    hidden_dims: list[int] = field(default_factory=lambda: [128, 64, 32])
    output_size: int = 1  # Signal strength prediction
    dropout: float = 0.2
    learning_rate: float = 0.001
    batch_size: int = 256
    epochs: int = 100


class SignalPredictor(nn.Module):
    """Neural network for signal strength prediction.

    Architecture:
    - Input: [batch, window_size, num_features]
    - LSTM layers for temporal pattern recognition
    - Fully connected layers for signal strength output
    - Output: [batch, 1] (signal strength 0-1)
    """

    def __init__(self, config: Optional[ModelConfig] = None):
        super().__init__()
        self.config = config or ModelConfig()

        # Calculate input size per timestep
        self.num_features = 13  # Based on FeatureEngine defaults

        # LSTM layer for temporal patterns
        self.lstm = nn.LSTM(
            input_size=self.num_features,
            hidden_size=self.config.hidden_dims[0],
            num_layers=2,
            batch_first=True,
            dropout=self.config.dropout,
        )

        # Fully connected layers
        fc_layers = []
        prev_dim = self.config.hidden_dims[0]

        for hidden_dim in self.config.hidden_dims[1:]:
            fc_layers.extend(
                [
                    nn.Linear(prev_dim, hidden_dim),
                    nn.ReLU(),
                    nn.Dropout(self.config.dropout),
                ]
            )
            prev_dim = hidden_dim

        self.fc = nn.Sequential(*fc_layers)

        # Output layer
        self.output = nn.Linear(prev_dim, self.config.output_size)
        self.sigmoid = nn.Sigmoid()

        # Loss function
        self.loss_fn = nn.MSELoss()

        # Optimizer
        self.optimizer = torch.optim.Adam(
            self.parameters(),
            lr=self.config.learning_rate,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass.

        Args:
            x: Input tensor [batch, window_size, num_features]

        Returns:
            Signal strength predictions [batch, 1]

        """
        # LSTM expects [batch, seq, features]
        lstm_out, (hidden, cell) = self.lstm(x)

        # Use last LSTM output
        last_output = lstm_out[:, -1, :]

        # Fully connected layers
        fc_out = self.fc(last_output)

        # Output with sigmoid (0-1 range)
        return self.sigmoid(self.output(fc_out))

    def predict(self, features: np.ndarray) -> float:
        """Predict signal strength from features.

        Args:
            features: Feature array [window_size, num_features]

        Returns:
            Signal strength (0-1)

        """
        self.eval()
        with torch.no_grad():
            x = torch.FloatTensor(features).unsqueeze(0)  # Add batch dimension
            prediction = self.forward(x)
            return prediction.item()

    def train_model(
        self,
        train_features: np.ndarray,
        train_labels: np.ndarray,
        val_features: Optional[np.ndarray] = None,
        val_labels: Optional[np.ndarray] = None,
    ) -> dict:
        """Train the model on features and labels.

        Args:
            train_features: Training features [num_samples, window_size, num_features]
            train_labels: Training labels [num_samples]
            val_features: Optional validation features
            val_labels: Optional validation labels

        Returns:
            Training history dict

        """
        self.train()

        # Convert to tensors
        X_train = torch.FloatTensor(train_features)
        y_train = torch.FloatTensor(train_labels).unsqueeze(1)

        dataset = torch.utils.data.TensorDataset(X_train, y_train)
        dataloader = torch.utils.data.DataLoader(
            dataset,
            batch_size=self.config.batch_size,
            shuffle=True,
        )

        history = {
            "train_loss": [],
            "val_loss": [],
        }

        for epoch in range(self.config.epochs):
            epoch_loss = 0.0
            num_batches = 0

            for batch_x, batch_y in dataloader:
                # Forward pass
                predictions = self.forward(batch_x)
                loss = self.loss_fn(predictions, batch_y)

                # Backward pass
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                epoch_loss += loss.item()
                num_batches += 1

            avg_loss = epoch_loss / num_batches
            history["train_loss"].append(avg_loss)

            # Validation
            if val_features is not None and val_labels is not None:
                val_loss = self._evaluate(val_features, val_labels)
                history["val_loss"].append(val_loss)
                logger.info(
                    f"Epoch {epoch + 1}/{self.config.epochs}, train_loss={avg_loss:.4f}, val_loss={val_loss:.4f}"
                )
            else:
                logger.info(f"Epoch {epoch + 1}/{self.config.epochs}, loss={avg_loss:.4f}")

        return history

    def _evaluate(self, features: np.ndarray, labels: np.ndarray) -> float:
        """Evaluate model on validation set."""
        self.eval()
        with torch.no_grad():
            X = torch.FloatTensor(features)
            y = torch.FloatTensor(labels).unsqueeze(1)
            predictions = self.forward(X)
            return self.loss_fn(predictions, y).item()

    def save(self, path: str) -> None:
        """Save model to disk."""
        torch.save(
            {
                "model_state": self.state_dict(),
                "optimizer_state": self.optimizer.state_dict(),
                "config": self.config,
            },
            path,
        )
        logger.info(f"Model saved to {path}")

    def load(self, path: str) -> None:
        """Load model from disk."""
        checkpoint = torch.load(path)
        self.load_state_dict(checkpoint["model_state"])
        self.optimizer.load_state_dict(checkpoint["optimizer_state"])
        logger.info(f"Model loaded from {path}")


class EnsemblePredictor:
    """Ensemble of multiple SignalPredictor models for improved accuracy."""

    def __init__(self, models: list[SignalPredictor], weights: Optional[list[float]] = None):
        self.models = models
        self.weights = weights or [1.0 / len(models)] * len(models)

    def predict(self, features: np.ndarray) -> float:
        """Ensemble prediction (weighted average)."""
        predictions = []
        for model in self.models:
            pred = model.predict(features)
            predictions.append(pred)

        # Weighted average
        return sum(p * w for p, w in zip(predictions, self.weights))

    def train_ensemble(
        self,
        train_features: np.ndarray,
        train_labels: np.ndarray,
        val_features: Optional[np.ndarray] = None,
        val_labels: Optional[np.ndarray] = None,
    ) -> list[dict]:
        """Train all models in ensemble."""
        histories = []
        for i, model in enumerate(self.models):
            logger.info(f"Training model {i + 1}/{len(self.models)}")
            history = model.train_model(train_features, train_labels, val_features, val_labels)
            histories.append(history)
        return histories
