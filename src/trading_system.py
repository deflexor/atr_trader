"""Unified trading system wiring together exchange data, ML, and backtesting.

Orchestrates the full flow:
1. Exchange adapter fetches historical data
2. Feature engineering creates ML features
3. ML model trains on historical data
4. Backtest runs with ML-enhanced signal predictions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Awaitable
import logging

from .adapters.kucoin_adapter import KuCoinAdapter
from .adapters.bybit_adapter import BybitAdapter
from .core.models.candle import Candle, CandleSeries
from .core.models.signal import Signal, SignalDirection
from .core.models.market_data import MarketData
from .ml.features import FeatureEngine, FeatureConfig
from .ml.model import SignalPredictor, ModelConfig
from .ml.training import TrainingPipeline, TrainingConfig
from .strategies.base_strategy import BaseStrategy
from .backtest.engine import BacktestEngine, BacktestConfig

logger = logging.getLogger(__name__)


@dataclass
class TradingSystemConfig:
    """Configuration for the unified trading system."""

    exchange: str = "kucoin"
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    timeframe: str = "1m"
    lookback_candles: int = 1000  # Historical candles for training
    feature_window: int = 60
    prediction_horizon: int = 5


class MLEnhancedSignal:
    """Wrapper that combines strategy signals with ML predictions."""

    def __init__(
        self,
        base_signal: Signal,
        ml_strength: float,
        ml_confidence: float,
    ):
        self.base_signal = base_signal
        self.ml_strength = ml_strength
        self.ml_confidence = ml_confidence

    @property
    def combined_strength(self) -> float:
        """Combine strategy and ML signal strengths.

        Weighted average: 60% strategy, 40% ML
        """
        return (self.base_signal.strength * 0.6) + (self.ml_strength * 0.4)

    @property
    def is_actionable(self) -> bool:
        """Signal is actionable if combined strength meets threshold."""
        return self.combined_strength >= 0.6 and self.ml_confidence >= 0.5


class TradingSystem:
    """Unified trading system with ML-enhanced signal generation.

    Coordinates:
    - Exchange adapters for market data
    - Feature engineering for ML
    - Model training and prediction
    - Backtesting with realistic fills
    """

    def __init__(
        self,
        config: Optional[TradingSystemConfig] = None,
        exchange_adapter: Optional[KuCoinAdapter | BybitAdapter] = None,
    ):
        self.config = config or TradingSystemConfig()
        self.exchange = exchange_adapter or self._create_exchange_adapter()
        self.feature_engine = FeatureEngine(
            FeatureConfig(
                window_size=self.config.feature_window,
                horizon=self.config.prediction_horizon,
            )
        )
        self.model: Optional[SignalPredictor] = None
        self.training_pipeline: Optional[TrainingPipeline] = None
        self._is_trained = False

    def _create_exchange_adapter(self) -> KuCoinAdapter | BybitAdapter:
        """Create exchange adapter based on config."""
        if self.config.exchange == "bybit":
            return BybitAdapter()
        return KuCoinAdapter()

    async def fetch_historical_data(
        self,
        symbol: str,
        limit: int = 1000,
    ) -> CandleSeries:
        """Fetch historical candles from exchange.

        Args:
            symbol: Trading pair symbol
            limit: Number of candles to fetch

        Returns:
            CandleSeries with historical data
        """
        logger.info(f"Fetching {limit} candles for {symbol} from {self.config.exchange}")

        ohlcv_data = await self.exchange.fetch_ohlcv(
            symbol,
            timeframe=self.config.timeframe,
            limit=limit,
        )

        if not ohlcv_data:
            raise ValueError(f"No data returned for {symbol}")

        candles = []
        for item in ohlcv_data:
            # KuCoin format: [timestamp, open, close, high, low, volume]
            # Timestamps are returned as strings
            candle = Candle(
                symbol=symbol,
                exchange=self.config.exchange,
                timeframe=self.config.timeframe,
                timestamp=datetime.fromtimestamp(int(item[0]) / 1000),
                open=float(item[1]),
                close=float(item[2]),
                high=float(item[3]),
                low=float(item[4]),
                volume=float(item[5]),
            )
            candles.append(candle)

        series = CandleSeries(
            candles=candles,
            symbol=symbol,
            exchange=self.config.exchange,
            timeframe=self.config.timeframe,
        )

        logger.info(f"Fetched {len(candles)} candles for {symbol}")
        return series

    def train_model(self, candles: CandleSeries) -> dict:
        """Train the ML model on historical candle data.

        Args:
            candles: Historical candle data

        Returns:
            Training metrics and history
        """
        logger.info("Training ML model on historical data...")

        # Prepare training data from candles
        train_features, train_labels = self._prepare_training_data(candles)

        # Initialize training pipeline
        # Get actual number of features from feature engine
        actual_num_features = self.feature_engine.num_features
        self.training_pipeline = TrainingPipeline(
            TrainingConfig(
                feature_config=FeatureConfig(
                    window_size=self.config.feature_window,
                    horizon=self.config.prediction_horizon,
                ),
                model_config=ModelConfig(
                    num_features=actual_num_features,
                    hidden_dims=[128, 64, 32],
                    output_size=1,
                    learning_rate=0.001,
                    batch_size=256,
                    epochs=50,  # Reduced for demo, can increase
                ),
            )
        )

        # Train model
        self.model = SignalPredictor(self.training_pipeline.config.model_config)
        history = self.model.train_model(train_features, train_labels)

        self._is_trained = True
        logger.info("ML model training complete")

        return {
            "history": history,
            "num_samples": len(train_features),
        }

    def _prepare_training_data(
        self,
        candles: CandleSeries,
    ) -> tuple:
        """Convert candles to ML training format.

        Args:
            candles: Historical candles

        Returns:
            (features, labels) tuple for training
        """
        features = []
        labels = []

        window = self.config.feature_window
        horizon = self.config.prediction_horizon

        for i in range(window, len(candles.candles) - horizon):
            # Create window of candles for feature generation
            window_candles = candles.candles[i - window : i]
            future_candle = candles.candles[i + horizon]

            # Create features
            feature_matrix = self.feature_engine.create_features(
                CandleSeries(
                    candles=window_candles,
                    symbol=candles.symbol,
                    exchange=candles.exchange,
                    timeframe=candles.timeframe,
                )
            )

            # Calculate label (future return normalized)
            current_price = window_candles[-1].close
            future_price = future_candle.close
            label = (future_price - current_price) / current_price

            features.append(feature_matrix)
            labels.append(label)

        # Normalize labels to 0-1 range
        import numpy as np

        labels = np.array(labels)
        labels_min, labels_max = labels.min(), labels.max()
        if labels_max - labels_min > 0:
            labels = (labels - labels_min) / (labels_max - labels_min)
        else:
            labels = np.ones_like(labels) * 0.5

        return np.array(features), labels

    def predict_signal_strength(
        self,
        candles: CandleSeries,
        symbol: str,
    ) -> tuple[float, float]:
        """Predict signal strength using trained ML model.

        Args:
            candles: Recent candle history (must have at least window_size candles)
            symbol: Trading pair symbol

        Returns:
            (predicted_strength, confidence) tuple

        """
        if not self._is_trained or self.model is None:
            return 0.5, 0.3  # Neutral prediction if not trained

        # Create features from recent candles
        feature_matrix = self.feature_engine.create_features(candles)

        # Get prediction
        self.model.eval()
        import torch

        with torch.no_grad():
            features = torch.FloatTensor(feature_matrix).unsqueeze(0)
            prediction = self.model(features).item()

        # Confidence is based on prediction distance from 0.5
        confidence = 1.0 - abs(prediction - 0.5) * 2

        return prediction, confidence

    async def generate_ml_enhanced_signal(
        self,
        symbol: str,
        strategy: BaseStrategy,
        candles: CandleSeries,
    ) -> MLEnhancedSignal:
        """Generate trading signal combining strategy and ML predictions.

        Args:
            symbol: Trading pair symbol
            strategy: Base strategy for signal generation
            candles: Historical candle data

        Returns:
            MLEnhancedSignal with combined strength
        """
        # Get base signal from strategy
        base_signal = await strategy.generate_signal(symbol, candles)

        # Get ML prediction if model is trained
        if self._is_trained and self.model:
            ml_strength, ml_confidence = self.predict_signal_strength(candles, symbol)
        else:
            ml_strength = 0.5
            ml_confidence = 0.3

        return MLEnhancedSignal(
            base_signal=base_signal,
            ml_strength=ml_strength,
            ml_confidence=ml_confidence,
        )

    def create_backtest_engine(
        self,
        config: Optional[BacktestConfig] = None,
    ) -> BacktestEngine:
        """Create configured backtest engine.

        Args:
            config: Optional backtest configuration

        Returns:
            Configured BacktestEngine instance
        """
        return BacktestEngine(config or BacktestConfig())

    async def run_backtest_with_ml(
        self,
        strategy: BaseStrategy,
        candles: CandleSeries,
        initial_capital: float = 10000.0,
    ) -> dict:
        """Run backtest using ML-enhanced signal generation.

        Args:
            strategy: Trading strategy to backtest
            candles: Historical candle data
            initial_capital: Starting capital for backtest

        Returns:
            Backtest results with ML metrics
        """
        logger.info(f"Running ML-enhanced backtest with {len(candles.candles)} candles")

        backtest_engine = self.create_backtest_engine()

        # Create signal generator that uses ML
        async def ml_signal_generator(sym: str, c: CandleSeries) -> Signal:
            ml_signal = await self.generate_ml_enhanced_signal(sym, strategy, c)
            signal = ml_signal.base_signal
            # Enhance signal with ML prediction
            signal.strength = ml_signal.combined_strength
            signal.confidence = ml_signal.ml_confidence
            signal.predicted_outcome = ml_signal.ml_strength
            return signal

        # Run backtest
        result = await backtest_engine.run(
            candles,
            ml_signal_generator,
            initial_capital,
        )

        logger.info(
            f"Backtest complete: Return={result.total_return_pct:.2f}%, "
            f"Sharpe={result.sharpe_ratio:.2f}, Win Rate={result.win_rate:.1%}"
        )

        return {
            "total_return_pct": result.total_return_pct,
            "sharpe_ratio": result.sharpe_ratio,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "final_capital": result.final_capital,
        }

    async def train_and_backtest(
        self,
        strategy: BaseStrategy,
        symbol: str,
        train_candles: CandleSeries,
        backtest_candles: Optional[CandleSeries] = None,
        initial_capital: float = 10000.0,
    ) -> dict:
        """Train model and run backtest in one workflow.

        Args:
            strategy: Trading strategy to use
            symbol: Trading pair symbol
            train_candles: Historical data for training
            backtest_candles: Optional separate backtest data (uses train if None)
            initial_capital: Starting capital

        Returns:
            Combined training and backtest results
        """
        # Step 1: Train ML model
        logger.info("Step 1: Training ML model...")
        train_results = self.train_model(train_candles)

        # Step 2: Run backtest with trained model
        logger.info("Step 2: Running ML-enhanced backtest...")
        test_candles = backtest_candles or train_candles
        backtest_results = await self.run_backtest_with_ml(
            strategy,
            test_candles,
            initial_capital,
        )

        return {
            "training": train_results,
            "backtest": backtest_results,
            "ml_trained": self._is_trained,
        }


# Convenience function for quick experiments
async def quick_backtest(
    exchange: str = "kucoin",
    symbol: str = "BTCUSDT",
    strategy: Optional[BaseStrategy] = None,
    initial_capital: float = 10000.0,
) -> dict:
    """Quick backtest with default settings.

    Fetches data, trains model, and runs backtest.

    Args:
        exchange: Exchange name ('kucoin' or 'bybit')
        symbol: Trading pair
        strategy: Optional strategy (creates MomentumStrategy if None)
        initial_capital: Starting capital

    Returns:
        Combined results
    """
    from .strategies.momentum_strategy import MomentumStrategy, MomentumConfig

    # Create system
    config = TradingSystemConfig(
        exchange=exchange,
        symbols=[symbol],
        lookback_candles=1000,
    )
    system = TradingSystem(config)

    # Create default strategy if not provided
    if strategy is None:
        strategy = MomentumStrategy(MomentumConfig(name="backtest"))

    # Fetch historical data
    candles = await system.fetch_historical_data(symbol, limit=1000)

    # Train and backtest
    results = await system.train_and_backtest(
        strategy=strategy,
        symbol=symbol,
        train_candles=candles,
        initial_capital=initial_capital,
    )

    return results
