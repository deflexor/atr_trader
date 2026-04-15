"""Configuration module for loading settings from YAML files.

Loads configuration from config/base.yaml and environment-specific overrides.
Follows clean code principles: pure functions, immutability, explicit deps.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ExchangeConfig:
    """Exchange-specific configuration.

    Immutable configuration for exchange API connections.
    """

    enabled: bool = False
    websocket_url: str = ""
    rest_url: str = ""
    rate_limit: int = 100


@dataclass(frozen=True)
class DatabaseConfig:
    """Database configuration settings."""

    path: str = "data/trading.db"
    backup_enabled: bool = True
    backup_interval_seconds: int = 3600


@dataclass(frozen=True)
class TradingConfig:
    """Trading engine configuration."""

    max_position_size: float = 0.1
    max_total_exposure: float = 0.5
    default_slippage_bps: int = 10
    order_timeout_seconds: int = 30


@dataclass(frozen=True)
class StrategyConfig:
    """Strategy framework configuration."""

    parallel_enabled: bool = True
    pyramid_enabled: bool = True
    max_pyramid_entries: int = 2
    volatility_window: int = 20
    max_volatility_threshold: float = 0.05


@dataclass(frozen=True)
class BacktestConfig:
    """Backtesting engine configuration."""

    initial_capital: float = 100000
    commission_bps: int = 5
    slippage_model: str = "volume_based"
    data_start_date: str = "2024-01-01"


@dataclass(frozen=True)
class MlConfig:
    """Machine learning configuration."""

    feature_window: int = 100
    prediction_horizon: int = 10
    model_save_path: str = "models/signal_predictor.pt"
    training_batch_size: int = 256
    epochs: int = 100


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    format: str = "json"
    output: str = "stdout"
    file_path: str = "logs/trading.log"


@dataclass(frozen=True)
class AppConfig:
    """Root application configuration.

    Aggregates all subsystem configurations into a single immutable config object.
    """

    exchanges: dict[str, ExchangeConfig] = field(default_factory=dict)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    strategies: StrategyConfig = field(default_factory=StrategyConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    ml: MlConfig = field(default_factory=MlConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _dict_to_exchange_config(data: dict[str, Any]) -> ExchangeConfig:
    """Convert dictionary to ExchangeConfig (pure function)."""
    return ExchangeConfig(
        enabled=data.get("enabled", False),
        websocket_url=data.get("websocket_url", ""),
        rest_url=data.get("rest_url", ""),
        rate_limit=data.get("rate_limit", 100),
    )


def _parse_exchanges(data: dict[str, Any]) -> dict[str, ExchangeConfig]:
    """Parse exchange configurations (pure function)."""
    exchanges: dict[str, ExchangeConfig] = {}
    for name, config_data in data.get("exchanges", {}).items():
        exchanges[name] = _dict_to_exchange_config(config_data)
    return exchanges


def _parse_database(data: dict[str, Any]) -> DatabaseConfig:
    """Parse database configuration (pure function)."""
    db_data = data.get("database", {})
    return DatabaseConfig(
        path=db_data.get("path", "data/trading.db"),
        backup_enabled=db_data.get("backup_enabled", True),
        backup_interval_seconds=db_data.get("backup_interval_seconds", 3600),
    )


def _parse_trading(data: dict[str, Any]) -> TradingConfig:
    """Parse trading configuration (pure function)."""
    trading_data = data.get("trading", {})
    return TradingConfig(
        max_position_size=trading_data.get("max_position_size", 0.1),
        max_total_exposure=trading_data.get("max_total_exposure", 0.5),
        default_slippage_bps=trading_data.get("default_slippage_bps", 10),
        order_timeout_seconds=trading_data.get("order_timeout_seconds", 30),
    )


def _parse_strategies(data: dict[str, Any]) -> StrategyConfig:
    """Parse strategy configuration (pure function)."""
    strat_data = data.get("strategies", {})
    return StrategyConfig(
        parallel_enabled=strat_data.get("parallel_enabled", True),
        pyramid_enabled=strat_data.get("pyramid_enabled", True),
        max_pyramid_entries=strat_data.get("max_pyramid_entries", 2),
        volatility_window=strat_data.get("volatility_window", 20),
        max_volatility_threshold=strat_data.get("max_volatility_threshold", 0.05),
    )


def _parse_backtest(data: dict[str, Any]) -> BacktestConfig:
    """Parse backtest configuration (pure function)."""
    bt_data = data.get("backtest", {})
    return BacktestConfig(
        initial_capital=bt_data.get("initial_capital", 100000),
        commission_bps=bt_data.get("commission_bps", 5),
        slippage_model=bt_data.get("slippage_model", "volume_based"),
        data_start_date=bt_data.get("data_start_date", "2024-01-01"),
    )


def _parse_ml(data: dict[str, Any]) -> MlConfig:
    """Parse ML configuration (pure function)."""
    ml_data = data.get("ml", {})
    return MlConfig(
        feature_window=ml_data.get("feature_window", 100),
        prediction_horizon=ml_data.get("prediction_horizon", 10),
        model_save_path=ml_data.get("model_save_path", "models/signal_predictor.pt"),
        training_batch_size=ml_data.get("training_batch_size", 256),
        epochs=ml_data.get("epochs", 100),
    )


def _parse_logging(data: dict[str, Any]) -> LoggingConfig:
    """Parse logging configuration (pure function)."""
    log_data = data.get("logging", {})
    return LoggingConfig(
        level=log_data.get("level", "INFO"),
        format=log_data.get("format", "json"),
        output=log_data.get("output", "stdout"),
        file_path=log_data.get("file_path", "logs/trading.log"),
    )


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from YAML file.

    Pure function that reads a YAML file and returns an immutable AppConfig.
    If no path provided, uses default config/base.yaml relative to project root.

    Args:
        config_path: Optional path to YAML config file.

    Returns:
        Immutable AppConfig with all subsystem configurations.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        yaml.YAMLError: If config file is invalid YAML.
    """
    if config_path is None:
        # Default to config/base.yaml in project root
        config_path = Path(__file__).parent.parent.parent / "config" / "base.yaml"

    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}

    return AppConfig(
        exchanges=_parse_exchanges(data),
        database=_parse_database(data),
        trading=_parse_trading(data),
        strategies=_parse_strategies(data),
        backtest=_parse_backtest(data),
        ml=_parse_ml(data),
        logging=_parse_logging(data),
    )


def get_config() -> AppConfig:
    """Get application configuration.

    Cached config instance for application-wide access.
    Uses environment variable CONFIG_PATH if set.
    """
    env_path = os.environ.get("CONFIG_PATH")
    return load_config(env_path)
