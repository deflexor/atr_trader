"""Meta-Labeling Model for Signal Filtering

From "Advances in Financial Machine Learning" by Marcos López de Prado.

Meta-labeling:
- Primary model generates candidate signals (momentum strategy)
- Secondary model learns when to ACCEPT or REJECT those signals
- Goal: higher precision by filtering low-quality signals

The MetaLabelModel is trained on historical trade outcomes:
- Label = 1 if trade was profitable (win)
- Label = 0 if trade was losing (loss)

Features capture signal quality indicators:
- Signal strength, confidence
- H1Model trend alignment
- Market volatility regime
- Volume conditions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple, List
import logging

import torch
import torch.nn as nn
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class MetaLabelConfig:
    """Configuration for meta-labeling model."""

    num_features: int = 6  # signal_str, conf, h1_dir, h1_conf, atr_pct, vol_spike
    hidden_dims: list[int] = field(default_factory=lambda: [32, 16])
    dropout: float = 0.3
    learning_rate: float = 0.001
    batch_size: int = 64
    epochs: int = 10
    accept_threshold: float = 0.5  # Probability threshold for accepting signals


class MetaLabelClassifier(nn.Module):
    """Binary classifier for meta-labeling (accept/reject signals).

    Simple fully-connected network for fast inference.
    """

    def __init__(self, config: Optional[MetaLabelConfig] = None):
        super().__init__()
        self.config = config or MetaLabelConfig()

        layers = []
        prev_dim = self.config.num_features

        for hidden_dim in self.config.hidden_dims:
            layers.extend([
                nn.Linear(prev_dim, hidden_dim),
                nn.ReLU(),
                nn.BatchNorm1d(hidden_dim),
                nn.Dropout(self.config.dropout),
            ])
            prev_dim = hidden_dim

        layers.append(nn.Linear(prev_dim, 1))
        layers.append(nn.Sigmoid())

        self.network = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass returns accept probability."""
        return self.network(x)


@dataclass
class TradeFeatures:
    """Features extracted from a trade for meta-labeling."""

    signal_strength: float      # 0-1, momentum indicator strength
    signal_confidence: float  # 0-1, confidence of signal
    h1_direction: int         # 0=DOWN, 1=FLAT, 2=UP
    h1_confidence: float      # 0-1, H1Model confidence
    atr_pct: float            # ATR as % of price (volatility regime)
    volume_spike: float       # Volume relative to average (1.0 = average)

    def to_array(self) -> np.ndarray:
        """Convert to feature array for model input."""
        return np.array([
            self.signal_strength,
            self.signal_confidence,
            self.h1_direction / 2.0,  # Normalize to 0-1
            self.h1_confidence,
            self.atr_pct / 0.01,  # Normalize: 1% ATR = 1.0
            self.volume_spike / 2.0,  # Normalize: 2x avg = 1.0
        ], dtype=np.float32)


@dataclass
class LabeledTrade:
    """A trade with features and outcome label."""

    features: TradeFeatures
    label: int  # 1 = profitable (ACCEPT was correct), 0 = losing (REJECT was correct)


class MetaLabelModel:
    """Meta-labeling model for signal quality filtering.

    Learns from historical trades which signal patterns tend to be profitable.
    Uses prediction to filter primary model signals.
    """

    def __init__(self, config: Optional[MetaLabelConfig] = None):
        self.config = config or MetaLabelConfig()
        self.model = MetaLabelClassifier(self.config)
        self.optimizer = torch.optim.AdamW(
            self.parameters(),
            lr=self.config.learning_rate,
            weight_decay=1e-4,
        )
        self._is_trained = False

    def parameters(self):
        """Return model parameters."""
        return self.model.parameters()

    @property
    def is_trained(self) -> bool:
        """Check if model has been trained."""
        return self._is_trained

    def train(
        self,
        trades: List[LabeledTrade],
        val_split: float = 0.2,
    ) -> dict:
        """Train meta-labeling model on labeled trades.

        Args:
            trades: List of LabeledTrade with features and outcome
            val_split: Fraction of data for validation

        Returns:
            Training history dict
        """
        if len(trades) < 10:
            logger.warning("Insufficient trades for training, need at least 10")
            return {"train_loss": [], "val_loss": [], "train_acc": [], "val_acc": []}

        # Prepare data
        features_list = []
        labels_list = []
        for trade in trades:
            features_list.append(trade.features.to_array())
            labels_list.append(float(trade.label))

        X = torch.FloatTensor(np.array(features_list))
        y = torch.FloatTensor(labels_list).unsqueeze(1)

        # Split train/val
        n = len(trades)
        n_val = int(n * val_split)
        indices = torch.randperm(n)

        X_train = X[indices[n_val:]]
        y_train = y[indices[n_val:]]
        X_val = X[indices[:n_val]]
        y_val = y[indices[:n_val]]

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

        self.model.train()

        for epoch in range(self.config.epochs):
            epoch_loss = 0.0
            correct = 0
            total = 0

            for batch_x, batch_y in dataloader:
                predictions = self.model(batch_x)
                loss = nn.BCELoss()(predictions, batch_y)

                self.optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.parameters(), 1.0)
                self.optimizer.step()

                epoch_loss += loss.item()
                predicted = (predictions >= 0.5).float()
                correct += (predicted == batch_y).sum().item()
                total += batch_y.size(0)

            avg_loss = epoch_loss / len(dataloader)
            acc = correct / total if total > 0 else 0
            history["train_loss"].append(avg_loss)
            history["train_acc"].append(acc)

            # Validation
            if len(X_val) > 0:
                val_loss, val_acc = self._evaluate(X_val, y_val)
                history["val_loss"].append(val_loss)
                history["val_acc"].append(val_acc)
                logger.info(
                    f"MetaLabel epoch {epoch+1}/{self.config.epochs}, "
                    f"loss={avg_loss:.4f}, acc={acc:.3f}, "
                    f"val_loss={val_loss:.4f}, val_acc={val_acc:.3f}"
                )
            else:
                logger.info(f"MetaLabel epoch {epoch+1}/{self.config.epochs}, loss={avg_loss:.4f}, acc={acc:.3f}")

        self._is_trained = True
        return history

    def _evaluate(self, X: torch.Tensor, y: torch.Tensor) -> Tuple[float, float]:
        """Evaluate on validation set."""
        self.model.eval()
        with torch.no_grad():
            predictions = self.model(X)
            loss = nn.BCELoss()(predictions, y).item()
            predicted = (predictions >= 0.5).float()
            acc = (predicted == y).float().mean().item()
        return loss, acc

    def should_accept(self, features: TradeFeatures) -> Tuple[bool, float]:
        """Predict whether to accept or reject a signal.

        Args:
            features: TradeFeatures extracted from signal

        Returns:
            (accept: bool, probability: float)
            accept: True if signal should be traded
            probability: Model's confidence in acceptance (0-1)
        """
        if not self._is_trained:
            # If not trained, accept all signals (no filtering)
            return True, 0.5

        self.model.eval()
        with torch.no_grad():
            x = torch.FloatTensor(features.to_array().reshape(1, -1))
            prob = self.model(x).item()

        accept = prob >= self.config.accept_threshold
        return accept, prob

    def predict(self, features: TradeFeatures) -> float:
        """Get acceptance probability for features."""
        _, prob = self.should_accept(features)
        return prob

    def save(self, path: str) -> None:
        """Save model to disk."""
        torch.save(
            {
                "model_state": self.model.state_dict(),
                "optimizer_state": self.optimizer.state_dict(),
                "config": self.config,
                "is_trained": self._is_trained,
            },
            path,
        )
        logger.info(f"MetaLabelModel saved to {path}")

    def load(self, path: str) -> None:
        """Load model from disk."""
        checkpoint = torch.load(path, weights_only=False)
        self.model.load_state_dict(checkpoint["model_state"])
        self.optimizer.load_state_dict(checkpoint["optimizer_state"])
        self.config = checkpoint["config"]
        self._is_trained = checkpoint.get("is_trained", True)
        logger.info(f"MetaLabelModel loaded from {path}")


def extract_features_from_trade(
    signal_strength: float,
    signal_confidence: float,
    h1_direction: int,
    h1_confidence: float,
    atr_pct: float,
    volume_spike: float,
) -> TradeFeatures:
    """Factory function to create TradeFeatures."""
    return TradeFeatures(
        signal_strength=signal_strength,
        signal_confidence=signal_confidence,
        h1_direction=h1_direction,
        h1_confidence=h1_confidence,
        atr_pct=atr_pct,
        volume_spike=volume_spike,
    )


def create_labeled_trades_from_backtest(
    trades: list[dict],
    features_extractor: callable,
) -> List[LabeledTrade]:
    """Create labeled trades from backtest results for training.

    Args:
        trades: List of trade dicts with 'pnl' field
        features_extractor: Function(trade) -> TradeFeatures

    Returns:
        List of LabeledTrade suitable for training
    """
    labeled_trades = []

    for trade in trades:
        if "pnl" not in trade:
            continue

        features = features_extractor(trade)
        label = 1 if trade["pnl"] > 0 else 0

        labeled_trades.append(LabeledTrade(features=features, label=label))

    return labeled_trades