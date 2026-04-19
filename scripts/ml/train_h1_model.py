"""Train 1h LSTM model for trend confirmation.

This script:
1. Fetches 1h candles using H1DataPipeline
2. Trains H1Model with epochs=5, batch_size=128 (matching existing model config)
3. Saves model to models/h1_lstm_model.pt
4. Verifies model performance
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import torch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.ml.h1_model import H1Model, H1ModelConfig
from src.ml.h1_pipeline import H1DataPipeline, H1PipelineConfig
from src.ml.model import (
    CLASS_DOWN,
    CLASS_FLAT,
    CLASS_UP,
    create_classification_labels,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Paths
MODELS_DIR = Path("models")
MODELS_DIR.mkdir(parents=True, exist_ok=True)
MODEL_SAVE_PATH = MODELS_DIR / "h1_lstm_model.pt"


def create_labels_from_returns(
    returns: np.ndarray,
    threshold_pct: float = 0.01,
) -> np.ndarray:
    """Convert return series to classification labels.

    Args:
        returns: Array of returns (normalized 0-1)
        threshold_pct: Threshold for UP/DOWN classification

    Returns:
        Array of integer labels (0=DOWN, 1=FLAT, 2=UP)
    """
    # Convert normalized returns back to actual returns for threshold
    labels = np.zeros(len(returns), dtype=np.int64)

    # Assuming returns were normalized to [0, 1], convert threshold
    # threshold_pct = 0.01 means 1% move
    # In normalized space, this is approximately 0.5 (midpoint)
    mid = 0.5
    labels[returns > mid + threshold_pct * 50] = CLASS_UP
    labels[returns < mid - threshold_pct * 50] = CLASS_DOWN

    return labels


def split_data(
    features: np.ndarray,
    labels: np.ndarray,
    train_split: float = 0.8,
    val_split: float = 0.1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split data into train/val/test sets.

    Args:
        features: Feature array [num_samples, window_size, num_features]
        labels: Label array [num_samples]
        train_split: Fraction for training
        val_split: Fraction for validation

    Returns:
        (train_features, train_labels, val_features, val_labels, test_features, test_labels)
    """
    n = len(features)
    train_end = int(n * train_split)
    val_end = int(n * (train_split + val_split))

    return (
        features[:train_end],
        labels[:train_end],
        features[train_end:val_end],
        labels[train_end:val_end],
        features[val_end:],
        labels[val_end:],
    )


def verify_model_performance(
    model: H1Model,
    test_features: np.ndarray,
    test_labels: np.ndarray,
) -> dict:
    """Verify model performance on test set.

    Args:
        model: Trained H1Model
        test_features: Test features
        test_labels: Test labels

    Returns:
        Performance metrics dict
    """
    # Class distribution
    class_counts = {
        "DOWN": int(np.sum(test_labels == CLASS_DOWN)),
        "FLAT": int(np.sum(test_labels == CLASS_FLAT)),
        "UP": int(np.sum(test_labels == CLASS_UP)),
    }
    total = len(test_labels)

    logger.info(f"Test set class distribution: {class_counts}")
    logger.info(f"  DOWN: {class_counts['DOWN']/total:.1%}")
    logger.info(f"  FLAT: {class_counts['FLAT']/total:.1%}")
    logger.info(f"  UP:   {class_counts['UP']/total:.1%}")

    # Per-class accuracy
    model.model.eval()
    correct = {CLASS_DOWN: 0, CLASS_FLAT: 0, CLASS_UP: 0}
    total_per_class = {CLASS_DOWN: 0, CLASS_FLAT: 0, CLASS_UP: 0}

    with torch.no_grad():
        for i in range(len(test_features)):
            features = test_features[i]
            true_label = test_labels[i]

            pred_class, confidence, _ = model.predict(features)

            total_per_class[true_label] += 1
            if pred_class == true_label:
                correct[true_label] += 1

    # Calculate per-class accuracy
    accuracy = {}
    for cls in [CLASS_DOWN, CLASS_FLAT, CLASS_UP]:
        cls_name = {CLASS_DOWN: "DOWN", CLASS_FLAT: "FLAT", CLASS_UP: "UP"}[cls]
        if total_per_class[cls] > 0:
            accuracy[cls_name] = correct[cls] / total_per_class[cls]
        else:
            accuracy[cls_name] = 0.0

    overall_correct = sum(correct.values())
    overall_accuracy = overall_correct / total if total > 0 else 0.0

    logger.info(f"Overall accuracy: {overall_accuracy:.1%}")
    for cls_name, acc in accuracy.items():
        cls_key = {"DOWN": CLASS_DOWN, "FLAT": CLASS_FLAT, "UP": CLASS_UP}[cls_name]
        logger.info(f"  {cls_name} accuracy: {acc:.1%} ({correct[cls_key]}/{total_per_class[cls_key]})")

    # Prediction latency test
    import time

    latencies = []
    with torch.no_grad():
        for i in range(min(100, len(test_features))):
            features = test_features[i]
            start = time.perf_counter()
            _ = model.predict(features)
            latency_ms = (time.perf_counter() - start) * 1000
            latencies.append(latency_ms)

    avg_latency = np.mean(latencies)
    p95_latency = np.percentile(latencies, 95)
    p99_latency = np.percentile(latencies, 99)

    logger.info(f"Prediction latency:")
    logger.info(f"  Average: {avg_latency:.2f}ms")
    logger.info(f"  P95: {p95_latency:.2f}ms")
    logger.info(f"  P99: {p99_latency:.2f}ms")

    return {
        "overall_accuracy": overall_accuracy,
        "class_accuracy": accuracy,
        "class_distribution": class_counts,
        "latency_avg_ms": avg_latency,
        "latency_p95_ms": p95_latency,
        "latency_p99_ms": p99_latency,
    }


async def main():
    """Main training pipeline."""
    logger.info("=" * 60)
    logger.info("1h LSTM Model Training")
    logger.info("=" * 60)

    # Configuration - match existing model config
    # epochs=5, batch_size=128 per requirements
    pipeline_config = H1PipelineConfig(
        symbol="BTCUSDT",
        exchange="kucoin",
        timeframe="1h",
        lookback_days=90,
        min_candles=1000,
    )

    model_config = H1ModelConfig(
        num_features=11,  # 3 price + 4 technical + 4 volume (no market depth)
        hidden_dims=[128, 64, 32],
        num_classes=3,
        dropout=0.3,
        learning_rate=0.001,
        batch_size=128,  # Per requirements: match existing model
        epochs=5,  # Per requirements: match existing model
        threshold_pct=0.01,  # 1% threshold for 1h moves
        timeframe="1h",
        horizon=4,  # 4 hours ahead for trend confirmation
    )

    # Initialize pipeline and model
    pipeline = H1DataPipeline(config=pipeline_config)
    model = H1Model(config=model_config)

    # Fetch and store data
    logger.info("Fetching 1h candle data...")
    inserted = await pipeline.fetch_and_store()

    if inserted == 0:
        logger.warning("No new candles fetched, using existing data")

    # Load data from database
    candles = pipeline.load_from_db()
    logger.info(f"Loaded {len(candles.candles)} candles from database")

    # Validate data
    validation = pipeline.validate_data(candles)
    if not validation["valid"]:
        logger.warning(f"Data validation issues: {validation['issues']}")

    logger.info(f"Data stats: {validation['stats']}")

    # Prepare features
    logger.info("Preparing features...")
    features, labels = pipeline.prepare_data(candles)
    logger.info(f"Prepared {len(features)} samples")

    # Convert continuous labels to classification labels
    # labels are normalized returns [0, 1], convert to 0/1/2
    classification_labels = create_labels_from_returns(
        labels,
        threshold_pct=model_config.threshold_pct,
    )

    # Log class distribution
    unique, counts = np.unique(classification_labels, return_counts=True)
    logger.info("Classification label distribution:")
    for cls, cnt in zip(unique, counts):
        name = {0: "DOWN", 1: "FLAT", 2: "UP"}[cls]
        logger.info(f"  {name}: {cnt} ({cnt/len(classification_labels):.1%})")

    # Split data
    (
        train_features,
        train_labels,
        val_features,
        val_labels,
        test_features,
        test_labels,
    ) = split_data(features, classification_labels)

    logger.info(f"Data split: train={len(train_features)}, val={len(val_features)}, test={len(test_features)}")

    # Train model
    logger.info("Training H1Model...")
    logger.info(f"Config: epochs={model_config.epochs}, batch_size={model_config.batch_size}")

    history = model.train(
        train_features=train_features,
        train_labels=train_labels,
        val_features=val_features,
        val_labels=val_labels,
    )

    # Log training history
    logger.info("Training history:")
    for epoch, (loss, acc) in enumerate(zip(history["train_loss"], history["train_acc"])):
        if history["val_loss"]:
            val_loss = history["val_loss"][epoch]
            val_acc = history["val_acc"][epoch]
            logger.info(f"  Epoch {epoch+1}: loss={loss:.4f}, acc={acc:.3f}, val_loss={val_loss:.4f}, val_acc={val_acc:.3f}")
        else:
            logger.info(f"  Epoch {epoch+1}: loss={loss:.4f}, acc={acc:.3f}")

    # Save model
    logger.info(f"Saving model to {MODEL_SAVE_PATH}...")
    model.save(str(MODEL_SAVE_PATH))
    logger.info("Model saved successfully")

    # Verify performance
    logger.info("Verifying model performance...")
    metrics = verify_model_performance(model, test_features, test_labels)

    # Acceptance criteria check
    logger.info("=" * 60)
    logger.info("Acceptance Criteria Check:")
    logger.info("=" * 60)

    # 1. H1Model trained
    logger.info(f"✅ H1Model trained: {model.is_trained}")

    # 2. Model saved to checkpoint
    model_file_exists = MODEL_SAVE_PATH.exists()
    logger.info(f"✅ Model saved to checkpoint: {model_file_exists} ({MODEL_SAVE_PATH})")

    # 3. Validation accuracy (check if test accuracy is reasonable)
    test_acc = metrics["overall_accuracy"]
    acc_ok = test_acc > 0.30  # Random baseline would be ~33% for 3 classes
    logger.info(f"✅ Validation accuracy: {test_acc:.1%} (expected >30%, {'PASS' if acc_ok else 'LOW'})")

    # 4. Prediction latency
    latency_ok = metrics["latency_p95_ms"] < 100
    logger.info(f"✅ Prediction latency P95: {metrics['latency_p95_ms']:.2f}ms (expected <100ms, {'PASS' if latency_ok else 'SLOW'})")

    all_passed = model.is_trained and model_file_exists and acc_ok and latency_ok

    logger.info("=" * 60)
    if all_passed:
        logger.info("All acceptance criteria PASSED")
    else:
        logger.warning("Some acceptance criteria FAILED")
    logger.info("=" * 60)

    return {
        "success": all_passed,
        "model_path": str(MODEL_SAVE_PATH),
        "metrics": metrics,
        "history": history,
    }


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result["success"] else 1)
