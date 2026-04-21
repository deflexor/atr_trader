"""Configuration loader for the trading bot.

Loads configuration from YAML files with environment variable overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TradingConfig:
    """Trading pair and exchange configuration."""

    exchanges: list[str] = field(default_factory=lambda: ["kucoin", "bybit"])
    pairs: list[str] = field(default_factory=lambda: ["BTC-USDT", "ETH-USDT"])
    timeframes: list[str] = field(default_factory=lambda: ["1m", "5m", "15m"])


@dataclass
class WebSocketConfig:
    """WebSocket connection configuration per exchange."""

    kucoin: dict[str, Any] = field(
        default_factory=lambda: {
            "ws_url": "wss://ws-api.kucoin.com/endpoint",
            "rest_url": "https://api.kucoin.com",
            "ping_interval": 30,
            "ping_timeout": 10,
        }
    )
    bybit: dict[str, Any] = field(
        default_factory=lambda: {
            "ws_url": "wss://stream.bybit.com/v5/public/spot",
            "rest_url": "https://api.bybit.com",
            "ping_interval": 20,
        }
    )


@dataclass
class DatabaseConfig:
    """Database configuration."""

    path: str = "data/trading.db"
    connection_timeout: int = 30
    pool_size: int = 5


@dataclass
class RiskConfig:
    """Risk management configuration."""

    max_position_size: float = 0.02
    stop_loss: float = 0.01
    take_profit: float = 0.02
    max_drawdown: float = 0.05

    # Zero-drawdown risk layer
    regime_detection: bool = True  # Enable GMM regime detection
    regime_lookback: int = 100  # Lookback for regime feature extraction
    boltzmann_temperature: float = 0.3  # Boltzmann thermal weighting sensitivity
    bootstrap_stops: bool = True  # Enable bootstrap stop calculation
    bootstrap_confidence: float = 0.95  # Confidence level for worst-case
    bootstrap_simulations: int = 1000  # Number of bootstrap resamples
    per_trade_drawdown_budget: float = 0.01  # 1% max drawdown per trade
    total_drawdown_budget: float = 0.03  # 3% total drawdown budget per session


@dataclass
class StrategyConfig:
    """Strategy framework configuration."""

    pyramid_entries: int = 2
    entry_spacing: float = 0.005
    volatility_filter: float = 0.02
    max_parallel: int = 2


@dataclass
class MLConfig:
    """Machine learning configuration."""

    feature_window: int = 60
    horizon: int = 5
    hidden_dims: list[int] = field(default_factory=lambda: [128, 64, 32])
    learning_rate: float = 0.001
    batch_size: int = 256
    epochs: int = 100


@dataclass
class BacktestConfig:
    """Backtesting configuration."""

    initial_capital: float = 10000
    commission: float = 0.001
    slippage_type: str = "volume"
    slippage_factor: float = 0.0005


@dataclass
class Config:
    """Root configuration object."""

    trading: TradingConfig = field(default_factory=TradingConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)


def load_config(config_dir: str | Path = "config") -> Config:
    """Load configuration from YAML files.

    Args:
        config_dir: Directory containing config files

    Returns:
        Config object with all settings

    """
    config_path = Path(config_dir)

    # Load base config
    base_file = config_path / "base.yaml"
    if base_file.exists():
        with open(base_file) as f:
            data = yaml.safe_load(f)
    else:
        data = {}

    # Override with environment variables
    def get_env(key: str, default: Any) -> Any:
        return os.environ.get(key, default)

    # Build config from loaded data
    return Config(
        trading=TradingConfig(**data.get("trading", {})),
        websocket=WebSocketConfig(**data.get("websocket", {})),
        database=DatabaseConfig(**data.get("database", {})),
        risk=RiskConfig(**data.get("risk", {})),
        strategy=StrategyConfig(**data.get("strategy", {})),
        ml=MLConfig(**data.get("ml", {})),
        backtest=BacktestConfig(**data.get("backtest", {})),
    )
