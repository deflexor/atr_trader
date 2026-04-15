"""Structured logging configuration for the trading bot.

Provides JSON-formatted structured logging with contextual information.
Follows clean code principles: pure functions, immutability, clear interfaces.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from config import LoggingConfig


def setup_logging(config: LoggingConfig) -> None:
    """Configure structured logging for the application.

    Sets up structlog with JSON output and contextual processors for
    structured logging throughout the trading bot.

    Args:
        config: Logging configuration from config module.

    Note:
        This function configures global logging state and should only
        be called once at application startup.
    """
    # Ensure log directory exists
    if config.file_path:
        log_path = Path(config.file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine log level
    log_level = getattr(logging, config.level.upper(), logging.INFO)

    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        stream=sys.stdout,
    )

    # Get root logger to configure handlers
    root_logger = logging.getLogger()

    # Add file handler if specified
    if config.file_path and config.output in ("stdout", "both"):
        file_handler = logging.FileHandler(config.file_path)
        file_handler.setLevel(log_level)
        root_logger.addHandler(file_handler)

    # Configure structlog processors
    processors = [
        # Add timestamp to all log entries
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        # Add context from existing extra dict
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if config.format == "json":
        # JSON output for production
        processors.append(structlog.processors.JSONRenderer())
    else:
        # Pretty console output for development
        processors.append(structlog.dev.ConsoleRenderer())

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance.

    Creates a named logger for a module with structured logging support.

    Args:
        name: Logger name, typically __name__ from the calling module.

    Returns:
        Structured logger bound to the given name.

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("order_filled", symbol="BTC", quantity=0.5, price=50000)
    """
    return structlog.get_logger(name)


class TradingLogger:
    """Structured logger with trading-specific context.

    Provides a convenient interface for logging trading events
    with consistent context information.
    """

    def __init__(
        self, name: str, strategy: str | None = None, exchange: str | None = None
    ):
        """Initialize trading logger with optional context.

        Args:
            name: Logger name (typically __name__).
            strategy: Optional strategy name for context.
            exchange: Optional exchange name for context.
        """
        self._logger = get_logger(name)
        self._context: dict = {}
        if strategy:
            self._context["strategy"] = strategy
        if exchange:
            self._context["exchange"] = exchange

    def _with_context(self, **kwargs) -> structlog.stdlib.BoundLogger:
        """Add context to logger (pure function)."""
        return self._logger.bind(**self._context, **kwargs)

    def order_submitted(
        self, symbol: str, side: str, quantity: float, price: float | None = None
    ) -> None:
        """Log order submission event."""
        extra = {"symbol": symbol, "side": side, "quantity": quantity}
        if price is not None:
            extra["price"] = price
        self._with_context(event="order_submitted", **extra)

    def order_filled(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        slippage_bps: float = 0,
    ) -> None:
        """Log order fill event."""
        self._with_context(
            event="order_filled",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            slippage_bps=slippage_bps,
        )

    def position_opened(
        self, symbol: str, side: str, quantity: float, entry_price: float
    ) -> None:
        """Log position opened event."""
        self._with_context(
            event="position_opened",
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
        )

    def position_closed(self, symbol: str, pnl: float, pnl_pct: float) -> None:
        """Log position closed event."""
        self._with_context(
            event="position_closed",
            symbol=symbol,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )

    def signal_generated(
        self, symbol: str, signal: str, confidence: float, features: dict | None = None
    ) -> None:
        """Log signal generation event."""
        extra: dict = {
            "symbol": symbol,
            "signal": signal,
            "confidence": confidence,
        }
        if features:
            extra["features"] = features
        self._with_context(event="signal_generated", **extra)

    def error(self, message: str, **kwargs) -> None:
        """Log error event with extra context."""
        self._with_context(event="error", message=message, **kwargs)

    def info(self, message: str, **kwargs) -> None:
        """Log info event with extra context."""
        self._with_context(event="info", message=message, **kwargs)

    def warning(self, message: str, **kwargs) -> None:
        """Log warning event with extra context."""
        self._with_context(event="warning", message=message, **kwargs)

    def debug(self, message: str, **kwargs) -> None:
        """Log debug event with extra context."""
        self._with_context(event="debug", message=message, **kwargs)
