"""Neural network model for signal direction classification.

Architecture:
- Input: [batch, window_size, num_features]
- LSTM layers for temporal pattern recognition
- Classification head with 3 outputs: DOWN(0), FLAT(1), UP(2)
- Output: class probabilities via softmax

Label definition:
- UP (2): future price rises by > threshold_pct over horizon steps
- DOWN (0): future price falls by > threshold_pct over horizon steps
- FLAT (1): everything else (no clear directional move)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple
import logging

import torch
import torch.nn as nn
import numpy as np

from .features import FeatureEngine, FeatureConfig

logger = logging.getLogger(__name__)


# Class indices
CLASS_DOWN = 0
CLASS_FLAT = 1
CLASS_UP = 2


@dataclass
class ModelConfig:
    """Neural network configuration."""

    num_features: int = 13  # features per timestep
    hidden_dims: list[int] = field(default_factory=lambda: [128, 64, 32])
    num_classes: int = 3  # DOWN, FLAT, UP
    dropout: float = 0.3  # higher dropout for classification
    learning_rate: float = 0.001
    batch_size: int = 256
    epochs: int = 50
    # Classification threshold: price must move > this fraction to count as UP/DOWN
    threshold_pct: float = 0.005


class SignalClassifier(nn.Module):
    """3-class LSTM classifier: DOWN / FLAT / UP.

    Uses softmax output and CrossEntropyLoss for proper classification.
    """

    def __init__(self, config: Optional[ModelConfig] = None):
        super().__init__()
        self.config = config or ModelConfig()

        self.num_features = getattr(self.config, "num_features", 13) or 13

        # Bidirectional LSTM for better pattern recognition
        self.lstm = nn.LSTM(
            input_size=self.num_features,
            hidden_size=self.config.hidden_dims[0],
            num_layers=2,
            batch_first=True,
            dropout=self.config.dropout,
            bidirectional=True,
        )

        # Attention-like pooling: weight last N outputs
        self.pool = nn.Linear(self.config.hidden_dims[0] * 2, 1)

        # Fully connected layers
        fc_layers = []
        prev_dim = self.config.hidden_dims[0] * 2

        for hidden_dim in self.config.hidden_dims[1:]:
            fc_layers.extend(
                [
                    nn.Linear(prev_dim, hidden_dim),
                    nn.ReLU(),
                    nn.BatchNorm1d(hidden_dim),
                    nn.Dropout(self.config.dropout),
                ]
            )
            prev_dim = hidden_dim

        self.fc = nn.Sequential(*fc_layers)

        # Classification head
        self.output = nn.Linear(prev_dim, self.config.num_classes)
        self.softmax = nn.Softmax(dim=1)

        # Loss and optimizer
        self.loss_fn = nn.CrossEntropyLoss()
        self.optimizer = torch.optim.AdamW(
            self.parameters(),
            lr=self.config.learning_rate,
            weight_decay=1e-4,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass.

        Args:
            x: Input tensor [batch, window_size, num_features]

        Returns:
            Class probabilities [batch, 3]
        """
        # LSTM
        lstm_out, _ = self.lstm(x)  # [batch, seq, hidden*2]

        # Weighted pooling over sequence (attention-like)
        weights = torch.softmax(self.pool(lstm_out), dim=1)  # [batch, seq, 1]
        pooled = (lstm_out * weights).sum(dim=1)  # [batch, hidden*2]

        # FC
        fc_out = self.fc(pooled)

        # Classification
        logits = self.output(fc_out)
        return self.softmax(logits)

    def predict_class(self, features: np.ndarray) -> Tuple[int, float, np.ndarray]:
        """Predict class and confidence from features.

        Args:
            features: Feature array [window_size, num_features]

        Returns:
            (class_id, confidence, all_probabilities)
            class_id: 0=DOWN, 1=FLAT, 2=UP
            confidence: probability of predicted class (0-1)
            all_probabilities: [p(DOWN), p(FLAT), p(UP)]
        """
        self.eval()
        with torch.no_grad():
            x = torch.FloatTensor(features).unsqueeze(0)
            probs = self.forward(x).squeeze(0).numpy()  # [3]
            class_id = int(np.argmax(probs))
            confidence = float(probs[class_id])
            return class_id, confidence, probs

    def train_model(
        self,
        train_features: np.ndarray,
        train_labels: np.ndarray,
        val_features: Optional[np.ndarray] = None,
        val_labels: Optional[np.ndarray] = None,
    ) -> dict:
        """Train classifier on features and labels.

        Args:
            train_features: Training features [num_samples, window_size, num_features]
            train_labels: Training labels [num_samples] with values 0, 1, 2
            val_features: Optional validation features
            val_labels: Optional validation labels

        Returns:
            Training history dict
        """
        self.train()

        X_train = torch.FloatTensor(train_features)  # features are float32
        y_train = torch.LongTensor(train_labels)  # labels are int (0,1,2)

        dataset = torch.utils.data.TensorDataset(X_train, y_train)
        dataloader = torch.utils.data.DataLoader(
            dataset,
            batch_size=self.config.batch_size,
            shuffle=True,
            drop_last=True,
        )

        history = {
            "train_loss": [],
            "train_acc": [],
            "val_loss": [],
            "val_acc": [],
        }

        for epoch in range(self.config.epochs):
            epoch_loss = 0.0
            correct = 0
            total = 0
            num_batches = 0

            for batch_x, batch_y in dataloader:
                predictions = self.forward(batch_x)  # [batch, 3]
                loss = self.loss_fn(predictions, batch_y)

                self.optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.parameters(), 1.0)
                self.optimizer.step()

                epoch_loss += loss.item()
                preds_class = torch.argmax(predictions, dim=1)
                correct += (preds_class == batch_y).sum().item()
                total += batch_y.size(0)
                num_batches += 1

            avg_loss = epoch_loss / num_batches
            acc = correct / total if total > 0 else 0
            history["train_loss"].append(avg_loss)
            history["train_acc"].append(acc)

            if val_features is not None and val_labels is not None:
                val_loss, val_acc = self._evaluate(val_features, val_labels)
                history["val_loss"].append(val_loss)
                history["val_acc"].append(val_acc)
                logger.info(
                    f"Epoch {epoch + 1}/{self.config.epochs}, "
                    f"loss={avg_loss:.4f}, acc={acc:.3f}, "
                    f"val_loss={val_loss:.4f}, val_acc={val_acc:.3f}"
                )
            else:
                logger.info(
                    f"Epoch {epoch + 1}/{self.config.epochs}, loss={avg_loss:.4f}, acc={acc:.3f}"
                )

        return history

    def _evaluate(self, features: np.ndarray, labels: np.ndarray) -> Tuple[float, float]:
        """Evaluate model on validation set."""
        self.eval()
        with torch.no_grad():
            X = torch.FloatTensor(features)  # features are float32
            y = torch.LongTensor(labels)  # labels are int (0,1,2)
            predictions = self.forward(X)
            loss = self.loss_fn(predictions, y).item()
            preds_class = torch.argmax(predictions, dim=1)
            acc = (preds_class == y).float().mean().item()
        return loss, acc

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


def create_classification_labels(
    candles_prices: np.ndarray,
    horizon: int,
    threshold_pct: float = 0.005,
) -> np.ndarray:
    """Create classification labels from price series.

    Labels:
        2 (UP): price rises by > threshold_pct over horizon
        0 (DOWN): price falls by > threshold_pct over horizon
        1 (FLAT): everything else

    Args:
        candles_prices: Array of close prices
        horizon: Number of steps ahead for prediction
        threshold_pct: Fraction that defines a significant move

    Returns:
        Array of integer labels [num_windows]
    """
    window = 60  # Must match FeatureConfig.window_size
    labels = []

    for i in range(window, len(candles_prices) - horizon):
        current = candles_prices[i]
        future = candles_prices[i + horizon]
        change_pct = (future - current) / current

        if change_pct > threshold_pct:
            labels.append(CLASS_UP)
        elif change_pct < -threshold_pct:
            labels.append(CLASS_DOWN)
        else:
            labels.append(CLASS_FLAT)

    return np.array(labels)


def class_to_direction(class_id: int) -> int:
    """Map classifier output to SignalDirection.

    Returns:
        2 (LONG) for UP, 0 (SHORT) for DOWN, 1 (NEUTRAL) for FLAT
    """
    if class_id == CLASS_UP:
        return 2  # LONG
    elif class_id == CLASS_DOWN:
        return 0  # SHORT
    return 1  # NEUTRAL
