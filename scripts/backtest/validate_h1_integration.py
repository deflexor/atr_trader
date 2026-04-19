"""Integration validation: H1Model with momentum strategy.

Validates:
1. H1Model loads into strategy
2. multi_timeframe_signal() works
3. Backtest runs with H1Model confirmation
4. Comparison baseline vs H1Model-enhanced
"""

from __future__ import annotations

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model, H1ModelConfig
from src.ml.model import CLASS_DOWN, CLASS_FLAT, CLASS_UP
from src.core.models.candle import Candle, CandleSeries
from src.core.models.signal import SignalDirection
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.trading_system import TradingSystem, TradingSystemConfig
import numpy as np


def create_synthetic_candles(n: int = 2000) -> CandleSeries:
    """Create synthetic candles for testing."""
    import random
    candles = []
    base_price = 50000.0
    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)

    for i in range(n):
        # Simulate BTC-like price movement
        change = random.gauss(0.0002, 0.01)  # Small upward drift
        base_price *= (1 + change)

        open_price = base_price
        close_price = base_price * (1 + random.gauss(0, 0.005))
        high_price = max(open_price, close_price) * (1 + abs(random.gauss(0, 0.003)))
        low_price = min(open_price, close_price) * (1 - abs(random.gauss(0, 0.003)))
        volume = random.uniform(100, 1000)

        candles.append(Candle(
            symbol="BTCUSDT",
            exchange="kucoin",
            timeframe="1m",
            timestamp=timestamp,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
        ))
        timestamp = timestamp + timedelta(minutes=1)

    return CandleSeries(
        candles=candles,
        symbol="BTCUSDT",
        exchange="kucoin",
        timeframe="1m",
    )


def create_synthetic_1h_candles(n: int = 100) -> CandleSeries:
    """Create synthetic 1h candles for H1Model testing."""
    import random
    candles = []
    base_price = 50000.0
    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)

    for i in range(n):
        # Simulate hourly price movement
        change = random.gauss(0.001, 0.02)  # Larger moves for 1h
        base_price *= (1 + change)

        open_price = base_price
        close_price = base_price * (1 + random.gauss(0, 0.01))
        high_price = max(open_price, close_price) * (1 + abs(random.gauss(0, 0.005)))
        low_price = min(open_price, close_price) * (1 - abs(random.gauss(0, 0.005)))
        volume = random.uniform(500, 5000)

        candles.append(Candle(
            symbol="BTCUSDT",
            exchange="kucoin",
            timeframe="1h",
            timestamp=timestamp,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
        ))
        timestamp = timestamp + timedelta(hours=1)

    return CandleSeries(
        candles=candles,
        symbol="BTCUSDT",
        exchange="kucoin",
        timeframe="1h",
    )


def train_dummy_h1_model(candles_1h: CandleSeries) -> H1Model:
    """Train a dummy H1Model on synthetic data for testing."""
    model = H1Model()

    # Create dummy features and labels
    window_size = 60
    n_samples = min(500, len(candles_1h.candles) - window_size - 4)

    if n_samples < 10:
        raise ValueError(f"Not enough samples: {n_samples}")

    features = []
    labels = []

    for i in range(n_samples):
        window = candles_1h.candles[i:i + window_size]
        # Create random features (11 features per timestep - no market depth)
        feat = np.random.randn(window_size, 11).astype(np.float32)
        features.append(feat)

        # Create random labels (0, 1, 2) as int64
        label = int(np.random.choice([CLASS_DOWN, CLASS_FLAT, CLASS_UP]))
        labels.append(label)

    train_features = np.array(features)
    train_labels = np.array(labels, dtype=np.int64)

    # Override batch size on the inner SignalClassifier for testing
    model.model.config.batch_size = min(32, n_samples)

    # Train
    history = model.train(train_features, train_labels)
    return model


async def run_backtest_comparison():
    """Run backtest comparing baseline vs H1Model-enhanced strategy."""
    print("=" * 60)
    print("H1Model Integration Validation")
    print("=" * 60)

    # Create synthetic 1m candles
    print("\n1. Creating synthetic 1m candles...")
    candles_1m = create_synthetic_candles(n=2000)
    print(f"   Created {len(candles_1m.candles)} 1m candles")

    # Create synthetic 1h candles
    print("\n2. Creating synthetic 1h candles for H1Model...")
    h1_candles = create_synthetic_1h_candles(n=100)
    print(f"   Created {len(h1_candles.candles)} 1h candles")

    # Train dummy H1Model
    print("\n3. Training dummy H1Model...")
    h1_model = train_dummy_h1_model(h1_candles)
    print(f"   H1Model trained: {h1_model.is_trained}")

    # Test strategy WITHOUT H1Model
    print("\n4. Testing baseline strategy (no H1Model)...")
    config_baseline = MomentumConfig(
        name="baseline",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0005,
    )
    strategy_baseline = MomentumStrategy(config=config_baseline)

    async def signal_gen_baseline(sym: str, c: CandleSeries) -> Signal:
        return await strategy_baseline.generate_signal(sym, c, None)

    engine_baseline = BacktestEngine(BacktestConfig(initial_capital=10000.0))
    result_baseline = await engine_baseline.run(candles_1m, signal_gen_baseline, 10000.0)

    print(f"   Baseline: {result_baseline.total_trades} trades, "
          f"win_rate={result_baseline.win_rate:.1%}, "
          f"return={result_baseline.total_return_pct:.2f}%, "
          f"drawdown={result_baseline.max_drawdown:.2f}%")

    # Test strategy WITH H1Model
    print("\n5. Testing H1Model-enhanced strategy...")
    config_h1 = MomentumConfig(
        name="h1_enhanced",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0005,
    )
    strategy_h1 = MomentumStrategy(config=config_h1, h1_model=h1_model)

    # Use multi_timeframe_signal path
    async def signal_gen_h1(sym: str, c: CandleSeries) -> Signal:
        signal, mtf_info = await strategy_h1.multi_timeframe_signal(sym, c, None)
        return signal

    engine_h1 = BacktestEngine(BacktestConfig(initial_capital=10000.0))
    result_h1 = await engine_h1.run(candles_1m, signal_gen_h1, 10000.0)

    print(f"   H1Model: {result_h1.total_trades} trades, "
          f"win_rate={result_h1.win_rate:.1%}, "
          f"return={result_h1.total_return_pct:.2f}%, "
          f"drawdown={result_h1.max_drawdown:.2f}%")

    # Compare
    print("\n6. Comparison Summary:")
    print("-" * 40)
    print(f"                      Baseline    H1Model-Enhanced")
    print(f"  Total Trades:        {result_baseline.total_trades:<12} {result_h1.total_trades}")
    print(f"  Win Rate:           {result_baseline.win_rate:.1%}       {result_h1.win_rate:.1%}")
    print(f"  Return:             {result_baseline.total_return_pct:.2f}%      {result_h1.total_return_pct:.2f}%")
    print(f"  Max Drawdown:       {result_baseline.max_drawdown:.2f}%      {result_h1.max_drawdown:.2f}%")
    print(f"  Sharpe:            {result_baseline.sharpe_ratio:.2f}        {result_h1.sharpe_ratio:.2f}")
    print("-" * 40)

    # Strategy diagnostics
    print("\n7. Strategy Diagnostics (H1Model-enhanced):")
    for key, value in strategy_h1.diagnostics.items():
        print(f"   {key}: {value}")

    # Test multi_timeframe_signal directly
    print("\n8. Testing multi_timeframe_signal() method...")
    signal, mtf_info = strategy_h1.multi_timeframe_signal("BTCUSDT", candles_1m)
    print(f"   Signal direction: {signal.direction}")
    print(f"   MTF info: {mtf_info}")

    results = {
        "baseline": {
            "total_trades": result_baseline.total_trades,
            "win_rate": result_baseline.win_rate,
            "total_return_pct": result_baseline.total_return_pct,
            "max_drawdown": result_baseline.max_drawdown,
            "sharpe_ratio": result_baseline.sharpe_ratio,
        },
        "h1_enhanced": {
            "total_trades": result_h1.total_trades,
            "win_rate": result_h1.win_rate,
            "total_return_pct": result_h1.total_return_pct,
            "max_drawdown": result_h1.max_drawdown,
            "sharpe_ratio": result_h1.sharpe_ratio,
        },
        "h1_model_filtered_count": strategy_h1.diagnostics.get("h1_model_filtered", 0),
        "mtf_info": mtf_info,
    }

    return results


if __name__ == "__main__":
    import asyncio

    results = asyncio.run(run_backtest_comparison())

    # Save results
    output_path = Path(".tmp/tasks/trading-bot-enhancement/integration_results.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nResults saved to {output_path}")