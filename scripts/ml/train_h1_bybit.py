"""Train H1Model on real Bybit 1h data.

Usage:
    python scripts/ml/train_h1_bybit.py --symbol BTCUSDT --days 90
"""

import asyncio
import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import torch

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.ml.h1_model import H1Model, H1ModelConfig
from src.ml.features import FeatureConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def fetch_1h_candles_bybit(symbol: str, days: int) -> CandleSeries:
    """Fetch 1h candles from Bybit."""
    adapter = BybitAdapter()
    
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    
    logger.info(f"Fetching {days} days of 1h candles from Bybit...")
    
    # Bybit uses "60" for 1h candles
    raw = await adapter.fetch_ohlcv_paginated(
        symbol, 
        timeframe="60",  # 1h = 60 minutes in Bybit
        limit=1000,
        start_time=start_time,
        end_time=end_time,
    )
    
    candles = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000  # Convert ms to seconds
            candles.append(Candle(
                symbol=symbol,
                exchange="bybit",
                timeframe="1h",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            ))
        except (ValueError, IndexError):
            continue
    
    candles.sort(key=lambda x: x.timestamp.timestamp())
    
    # Deduplicate
    seen = {}
    result = []
    for c in candles:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    
    logger.info(f"Fetched {len(result)} unique 1h candles")
    return CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1h")


def prepare_features(candles: CandleSeries, window_size: int = 60, horizon: int = 4) -> tuple:
    """Prepare features and labels from 1h candles."""
    from src.ml.features import FeatureEngine
    
    feature_engine = FeatureEngine(FeatureConfig(
        window_size=window_size,
        horizon=horizon,
        include_technical=True,
        include_volume=True,
        include_market_depth=False,  # No market depth for 1h
    ))
    
    features_list = []
    labels_list = []
    
    n = len(candles.candles)
    
    # Need window_size + horizon candles to create one sample
    max_start_idx = n - horizon - window_size
    
    for i in range(max_start_idx + 1):
        window = candles.candles[i:i + window_size]
        window_series = CandleSeries(
            candles=window,
            symbol=candles.symbol,
            exchange=candles.exchange,
            timeframe=candles.timeframe,
        )
        
        features = feature_engine.create_features(window_series)
        features_list.append(features)
        
        # Label: future return over horizon
        current_close = window[-1].close
        future_idx = i + window_size + horizon - 1
        if future_idx < n:
            future_close = candles.candles[future_idx].close
            label = (future_close - current_close) / current_close
        else:
            label = 0.0  # Fallback if not enough data
        labels_list.append(label)
    
    return np.array(features_list), np.array(labels_list)


def create_classification_labels(returns: np.ndarray, threshold_pct: float = 0.01) -> np.ndarray:
    """Convert returns to classification labels (DOWN/FLAT/UP)."""
    labels = np.zeros(len(returns), dtype=np.int64)
    
    # Find threshold in normalized space
    # returns are actual percentages, threshold_pct = 0.01 means 1%
    labels[returns > threshold_pct] = 2  # UP
    labels[returns < -threshold_pct] = 0  # DOWN
    # FLAT = 1 (default)
    
    return labels


async def main():
    parser = argparse.ArgumentParser(description="Train H1Model on Bybit 1h data")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--days", type=int, default=90, help="Days of historical data")
    parser.add_argument("--horizon", type=int, default=4, help="Prediction horizon (hours)")
    parser.add_argument("--output", default="models/h1_lstm_model.pt", help="Output model path")
    args = parser.parse_args()
    
    print("=" * 60)
    print("H1Model Training on Bybit 1h Data")
    print("=" * 60)
    print(f"Symbol:  {args.symbol}")
    print(f"Days:    {args.days}")
    print(f"Horizon: {args.horizon} hours")
    print("=" * 60)
    
    # Fetch 1h candles
    print("\n[1/5] Fetching 1h candles from Bybit...")
    candles = await fetch_1h_candles_bybit(args.symbol, args.days)
    if len(candles.candles) < 500:
        logger.error(f"Not enough candles: {len(candles.candles)}")
        return
    
    # Prepare features
    print("\n[2/5] Preparing features...")
    features, returns = prepare_features(candles, horizon=args.horizon)
    logger.info(f"Prepared {len(features)} samples, return range: [{returns.min():.4f}, {returns.max():.4f}]")
    
    # Create labels
    print("\n[3/5] Creating classification labels...")
    labels = create_classification_labels(returns, threshold_pct=0.01)
    
    unique, counts = np.unique(labels, return_counts=True)
    for cls, cnt in zip(unique, counts):
        name = {0: "DOWN", 1: "FLAT", 2: "UP"}[cls]
        logger.info(f"  {name}: {cnt} ({cnt/len(labels):.1%})")
    
    # Split data
    print("\n[4/5] Training model...")
    n = len(features)
    train_end = int(n * 0.8)
    val_end = int(n * 0.9)
    
    train_features = features[:train_end]
    train_labels = labels[:train_end]
    val_features = features[train_end:val_end]
    val_labels = labels[train_end:val_end]
    test_features = features[val_end:]
    test_labels = labels[val_end:]
    
    logger.info(f"Train: {len(train_features)}, Val: {len(val_features)}, Test: {len(test_features)}")
    
    # Create and train model
    config = H1ModelConfig(
        num_features=11,  # 3 price + 4 technical + 4 volume
        hidden_dims=[128, 64, 32],
        num_classes=3,
        dropout=0.3,
        learning_rate=0.001,
        batch_size=128,
        epochs=5,
        threshold_pct=0.01,
        timeframe="1h",
        horizon=args.horizon,
    )
    
    model = H1Model(config=config)
    history = model.train(
        train_features=train_features,
        train_labels=train_labels,
        val_features=val_features,
        val_labels=val_labels,
    )
    
    # Log training history
    logger.info("Training history:")
    for epoch, (loss, acc) in enumerate(zip(history["train_loss"], history["train_acc"])):
        if history.get("val_loss"):
            val_loss = history["val_loss"][epoch]
            val_acc = history["val_acc"][epoch]
            logger.info(f"  Epoch {epoch+1}: loss={loss:.4f}, acc={acc:.3f}, val_loss={val_loss:.4f}, val_acc={val_acc:.3f}")
    
    # Evaluate on test set
    print("\n[5/5] Evaluating on test set...")
    test_correct = 0
    test_total = len(test_features)
    
    for i in range(test_total):
        pred, conf, probs = model.predict(test_features[i])
        if pred == test_labels[i]:
            test_correct += 1
    
    test_acc = test_correct / test_total if test_total > 0 else 0
    logger.info(f"Test accuracy: {test_acc:.1%}")
    
    # Class breakdown
    for cls in [0, 1, 2]:
        cls_name = {0: "DOWN", 1: "FLAT", 2: "UP"}[cls]
        cls_total = np.sum(test_labels == cls)
        cls_correct = sum(1 for i in range(test_total) if test_labels[i] == cls and model.predict(test_features[i])[0] == cls)
        cls_acc = cls_correct / cls_total if cls_total > 0 else 0
        logger.info(f"  {cls_name}: {cls_acc:.1%} ({cls_correct}/{cls_total})")
    
    # Save model
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    model.save(str(output_path))
    logger.info(f"Model saved to {output_path}")
    
    print(f"\n✅ H1Model trained and saved to {output_path}")
    
    # Test prediction latency
    import time
    latencies = []
    for i in range(100):
        features_batch = test_features[i:i+1]
        start = time.perf_counter()
        _ = model.predict(features_batch[0])
        latencies.append((time.perf_counter() - start) * 1000)
    
    logger.info(f"Latency: avg={np.mean(latencies):.2f}ms, p95={np.percentile(latencies, 95):.2f}ms")


if __name__ == "__main__":
    asyncio.run(main())