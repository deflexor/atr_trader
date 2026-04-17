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
from .ml.model import (
    SignalClassifier,
    ModelConfig,
    create_classification_labels,
    class_to_direction,
    CLASS_UP,
    CLASS_DOWN,
)
from .ml.features import FeatureEngine, FeatureConfig
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
    """Wrapper combining strategy signals with ML classification.

    ML produces 3-class probabilities [p(DOWN), p(FLAT), p(UP)].

    Design: ML is an ENHANCER, not a gate.
    - When ML agrees with strategy → boost confidence/strength
    - When ML says FLAT → slightly reduce confidence (don't block)
    - When ML disagrees → reduce confidence more (don't block)

    The base strategy signal is always preserved — ML only adjusts the
    magnitude, never forces NEUTRAL. This avoids the zero-trade problem
    where ML's FLAT class (often dominant) killed all signals.
    """

    def __init__(
        self,
        base_signal: Signal,
        ml_strength: int,  # 0=DOWN, 1=FLAT, 2=UP (class_id)
        ml_confidence: float,
        ml_class_probs: np.ndarray,  # [p(DOWN), p(FLAT), p(UP)]
    ):
        self.base_signal = base_signal
        self.ml_strength = ml_strength  # class_id: 0=DOWN, 1=FLAT, 2=UP
        self.ml_confidence = ml_confidence
        self.ml_class_probs = ml_class_probs

    @property
    def combined_strength(self) -> float:
        """Signal strength adjusted by ML agreement.

        Boost when ML agrees, penalize when ML is neutral/disagrees.
        """
        base = self.base_signal.strength
        ml_multiplier = self._ml_agreement_multiplier()
        return min(1.0, base * ml_multiplier)

    @property
    def combined_confidence(self) -> float:
        """Confidence adjusted by ML agreement."""
        base = self.base_signal.confidence
        ml_multiplier = self._ml_agreement_multiplier()
        return min(1.0, base * ml_multiplier)

    @property
    def is_actionable(self) -> bool:
        """Signal is always actionable if the base strategy produced a signal.

        ML is an enhancer, not a gate. We never return False just because
        ML disagrees — that was causing zero trades. Instead, ML adjusts
        the strength/confidence, and the execution layer uses those to
        decide position sizing.
        """
        return self.base_signal.direction != SignalDirection.NEUTRAL

    def _ml_agreement_multiplier(self) -> float:
        """Calculate multiplier based on ML agreement with base signal.

        Returns:
            1.3  — ML strongly agrees (same direction, high prob)
            1.0  — ML says FLAT (no boost, no penalty)
            0.7  — ML disagrees (different direction)
        """
        base_dir = self.base_signal.direction  # SignalDirection enum
        ml_class = self.ml_strength
        ml_prob = self.ml_class_probs[ml_class] if self.ml_class_probs is not None else 0.33

        # Base strategy is neutral — no adjustment
        if base_dir == SignalDirection.NEUTRAL:
            return 1.0

        # Check direction agreement: LONG+UP or SHORT+DOWN
        agrees = (base_dir == SignalDirection.LONG and ml_class == 2) or (
            base_dir == SignalDirection.SHORT and ml_class == 0
        )

        if agrees and ml_prob > 0.5:
            return 1.3  # Strong agreement
        elif agrees:
            return 1.1  # Weak agreement
        elif ml_class == 1:
            return 1.0  # ML says FLAT — neutral, don't penalize
        else:
            return 0.7  # ML disagrees — reduce but don't kill


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
        """Fetch historical candles from exchange with pagination.

        Args:
            symbol: Trading pair symbol
            limit: Number of candles to fetch

        Returns:
            CandleSeries with historical data
        """
        logger.info(f"Fetching {limit} candles for {symbol} from {self.config.exchange}")

        # Use paginated fetch for larger datasets
        if hasattr(self.exchange, "fetch_ohlcv_paginated"):
            ohlcv_data = await self.exchange.fetch_ohlcv_paginated(
                symbol,
                timeframe=self.config.timeframe,
                limit=limit,
            )
        else:
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
        """Train the ML classifier on historical candle data.

        Args:
            candles: Historical candle data

        Returns:
            Training metrics and history
        """
        logger.info("Training ML classifier on historical data...")

        train_features, train_labels = self._prepare_classification_data(candles)

        if len(train_features) == 0:
            logger.warning("No training data available")
            return {"history": {}, "num_samples": 0}

        # Shuffle and split train/val
        import numpy as np

        indices = np.arange(len(train_features))
        np.random.shuffle(indices)
        split = int(len(indices) * 0.8)
        train_idx, val_idx = indices[:split], indices[split:]

        train_x = train_features[train_idx]
        train_y = train_labels[train_idx]
        val_x = train_features[val_idx]
        val_y = train_labels[val_idx]

        # Create classifier
        actual_num_features = self.feature_engine.num_features
        model_cfg = ModelConfig(
            num_features=actual_num_features,
            hidden_dims=[128, 64, 32],  # Full model
            num_classes=3,
            dropout=0.3,
            learning_rate=0.001,
            batch_size=128,
            epochs=5,  # Fast for CPU
            threshold_pct=0.005,
        )

        self.model = SignalClassifier(model_cfg)
        history = self.model.train_model(train_x, train_y, val_x, val_y)

        # Class distribution
        class_counts = np.bincount(train_labels, minlength=3)
        logger.info(
            f"Training class distribution: DOWN={class_counts[0]}, "
            f"FLAT={class_counts[1]}, UP={class_counts[2]}"
        )

        self._is_trained = True
        logger.info("ML classifier training complete")

        return {
            "history": history,
            "num_samples": len(train_features),
            "class_counts": class_counts.tolist(),
        }

    def _prepare_classification_data(self, candles: CandleSeries) -> tuple:
        """Convert candles to classification training format.

        Labels: 0=DOWN, 1=FLAT, 2=UP based on future price movement.

        Returns:
            (features, labels) tuple for training
        """
        import numpy as np

        window = self.config.feature_window
        horizon = self.config.prediction_horizon

        # Collect all close prices
        closes = np.array([c.close for c in candles.candles])

        # Generate classification labels
        raw_labels = create_classification_labels(closes, horizon=horizon, threshold_pct=0.005)

        features = []
        labels = []

        for i in range(window, len(candles.candles) - horizon):
            window_candles = candles.candles[i - window : i]

            feature_matrix = self.feature_engine.create_features(
                CandleSeries(
                    candles=window_candles,
                    symbol=candles.symbol,
                    exchange=candles.exchange,
                    timeframe=candles.timeframe,
                )
            )

            label_idx = i - window
            if label_idx < len(raw_labels):
                features.append(feature_matrix)
                labels.append(raw_labels[label_idx])

        return np.array(features), np.array(labels)

    def predict_signal_strength(
        self,
        candles: CandleSeries,
        symbol: str,
    ) -> tuple[int, float, np.ndarray]:
        """Predict signal direction using trained ML classifier.

        Args:
            candles: Recent candle history (must have at least window_size candles)
            symbol: Trading pair symbol

        Returns:
            (class_id, confidence, all_probabilities)
            class_id: 0=DOWN, 1=FLAT, 2=UP
            confidence: probability of predicted class
            all_probabilities: [p(DOWN), p(FLAT), p(UP)]
        """
        if not self._is_trained or self.model is None:
            return 1, 0.3, np.array([0.33, 0.34, 0.33])

        class_id, confidence, probs = self.model.predict_class(
            self.feature_engine.create_features(candles)
        )
        return class_id, confidence, probs

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
            class_id, ml_confidence, probs = self.predict_signal_strength(candles, symbol)
        else:
            class_id = 1
            ml_confidence = 0.3
            probs = np.array([0.33, 0.34, 0.33])

        # Tag base signal with LSTM confidence for downstream gating
        base_signal.ml_max_prob = float(probs.max())

        return MLEnhancedSignal(
            base_signal=base_signal,
            ml_strength=class_id,  # 0=DOWN, 1=FLAT, 2=UP
            ml_confidence=ml_confidence,
            ml_class_probs=probs,  # [p(DOWN), p(FLAT), p(UP)]
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

        # Create signal generator that uses ML classification as enhancer
        async def ml_signal_generator(sym: str, c: CandleSeries) -> Signal:
            ml_signal = await self.generate_ml_enhanced_signal(sym, strategy, c)
            signal = ml_signal.base_signal

            # If base strategy says NEUTRAL, nothing to enhance
            if signal.direction == SignalDirection.NEUTRAL:
                return signal

            # ML is an enhancer — adjust strength/confidence but never force NEUTRAL
            signal.strength = ml_signal.combined_strength
            signal.confidence = ml_signal.combined_confidence

            # When ML strongly agrees and base is neutral, let ML drive direction
            # (This handles the case where strategy didn't fire but ML sees a move)
            if signal.direction == SignalDirection.NEUTRAL and ml_signal.ml_strength != 1:
                if ml_signal.ml_strength == 2:  # UP
                    signal.direction = SignalDirection.LONG
                elif ml_signal.ml_strength == 0:  # DOWN
                    signal.direction = SignalDirection.SHORT
                signal.strength = (
                    ml_signal.combined_strength * 0.7
                )  # Reduced confidence for ML-only

            return signal

        # Run backtest
        result = await backtest_engine.run(
            candles,
            ml_signal_generator,
            initial_capital,
        )

        # Close any remaining open positions at the end of backtest
        # This ensures we count unrealized PnL as realized for the day's result
        if backtest_engine.positions and candles.candles:
            last_price = candles.candles[-1].close
            last_volume = candles.candles[-1].volume
            for position in backtest_engine.positions[:]:
                backtest_engine._close_position(
                    position, last_price, last_volume, "end_of_backtest"
                )

            # Recalculate final metrics after closing positions
            final_equity = backtest_engine._calculate_equity(last_price)
            total_return = final_equity - initial_capital
            total_return_pct = (total_return / initial_capital) * 100

            close_trades = [t for t in backtest_engine.trades if t.get("pnl") is not None]
            backtest_engine.metrics.calculate_from_trades(
                close_trades, backtest_engine.equity_curve
            )

            winning = [t for t in close_trades if t.get("pnl", 0) > 0]
            losing = [t for t in close_trades if t.get("pnl", 0) <= 0]

            result = type(result)(
                initial_capital=initial_capital,
                final_capital=final_equity,
                total_return=total_return,
                total_return_pct=total_return_pct,
                max_drawdown=backtest_engine.metrics.max_drawdown,
                sharpe_ratio=backtest_engine.metrics.sharpe_ratio,
                win_rate=len(winning) / len(close_trades) if close_trades else 0,
                total_trades=len(close_trades),
                winning_trades=len(winning),
                losing_trades=len(losing),
                avg_win=sum(t.get("pnl", 0) for t in winning) / len(winning) if winning else 0,
                avg_loss=sum(t.get("pnl", 0) for t in losing) / len(losing) if losing else 0,
                avg_trade_return=total_return / len(close_trades) if close_trades else 0,
                equity_curve=backtest_engine.equity_curve,
                trades=backtest_engine.trades,
                duration_seconds=result.duration_seconds,
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
