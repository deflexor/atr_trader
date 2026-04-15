"""Backtesting engine with historical data replay.

Provides realistic backtesting with slippage modeling and commission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Generator
import logging

from ..core.models.candle import Candle, CandleSeries
from ..core.models.signal import Signal, SignalDirection
from ..core.models.order import Order, OrderStatus, OrderSide, OrderType
from ..core.models.position import Position
from .fills import FillSimulator
from .metrics import PerformanceMetrics

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for backtesting."""

    initial_capital: float = 10000.0
    commission: float = 0.001  # 0.1% per trade
    slippage_type: str = "volume"  # volume or orderbook
    slippage_factor: float = 0.0005
    max_positions: int = 10


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    initial_capital: float
    final_capital: float
    total_return: float
    total_return_pct: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float
    avg_trade_return: float
    equity_curve: list[dict]
    trades: list[dict]
    duration_seconds: float


class BacktestEngine:
    """Backtesting engine for strategy evaluation.

    Replays historical data through a strategy and calculates
    realistic performance metrics including slippage and commissions.
    """

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        fill_simulator: Optional[FillSimulator] = None,
    ):
        self.config = config or BacktestConfig()
        self.fill_simulator = fill_simulator or FillSimulator(
            slippage_type=self.config.slippage_type,
            slippage_factor=self.config.slippage_factor,
        )
        self.metrics = PerformanceMetrics()

        # State
        self.capital = 0.0
        self.positions: list[Position] = []
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None

    def reset(self) -> None:
        """Reset backtest state for new run."""
        self.capital = self.config.initial_capital
        self.positions = []
        self.trades = []
        self.equity_curve = []
        self.start_time = None
        self.end_time = None
        self.metrics.reset()

    async def run(
        self,
        candles: CandleSeries,
        signal_generator: Callable[[str, CandleSeries], Signal],
        initial_capital: Optional[float] = None,
    ) -> BacktestResult:
        """Run backtest on historical data.

        Args:
            candles: Historical candle data
            signal_generator: Function that generates signals from candles
            initial_capital: Override initial capital

        Returns:
            BacktestResult with performance metrics

        """
        self.reset()
        if initial_capital:
            self.capital = initial_capital
        else:
            self.capital = self.config.initial_capital

        self.start_time = datetime.utcnow()
        logger.info(
            f"Starting backtest: {len(candles.candles)} candles, initial_capital={self.capital}"
        )

        # Process each candle
        for i, candle in enumerate(candles.candles):
            timestamp = candle.timestamp

            # Update positions with current price
            self._update_positions(candle.close)

            # Generate signal
            signal = await signal_generator(candles.symbol, candles)

            # Process signal
            if signal.is_actionable and signal.direction != SignalDirection.NEUTRAL:
                self._process_signal(signal, candle)

            # Record equity
            equity = self._calculate_equity(candle.close)
            self.equity_curve.append(
                {
                    "timestamp": timestamp,
                    "equity": equity,
                    "positions": len(self.positions),
                }
            )

        self.end_time = datetime.utcnow()
        duration = (self.end_time - self.start_time).total_seconds()

        # Calculate final metrics
        final_equity = self._calculate_equity(candles.candles[-1].close if candles.candles else 0)
        total_return = final_equity - self.config.initial_capital
        total_return_pct = (total_return / self.config.initial_capital) * 100

        # Get metrics from trades
        winning = [t for t in self.trades if t.get("pnl", 0) > 0]
        losing = [t for t in self.trades if t.get("pnl", 0) <= 0]

        return BacktestResult(
            initial_capital=self.config.initial_capital,
            final_capital=final_equity,
            total_return=total_return,
            total_return_pct=total_return_pct,
            max_drawdown=self.metrics.max_drawdown,
            sharpe_ratio=self.metrics.sharpe_ratio,
            win_rate=len(winning) / len(self.trades) if self.trades else 0,
            total_trades=len(self.trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            avg_win=sum(t["pnl"] for t in winning) / len(winning) if winning else 0,
            avg_loss=sum(t.get("pnl", 0) for t in losing) / len(losing) if losing else 0,
            avg_trade_return=total_return / len(self.trades) if self.trades else 0,
            equity_curve=self.equity_curve,
            trades=self.trades,
            duration_seconds=duration,
        )

    def _update_positions(self, current_price: float) -> None:
        """Update all positions with current price and check exits."""
        for position in self.positions[:]:  # Copy list to iterate safely
            position.update_price(current_price)

            # Check stop loss / take profit
            if position.is_stop_triggered() or position.is_tp_triggered():
                self._close_position(position, current_price, "exit")

    def _process_signal(self, signal: Signal, candle: Candle) -> None:
        """Process trading signal and execute orders."""
        # Check if we can open new position
        if len(self.positions) >= self.config.max_positions:
            return

        # Calculate quantity if not set
        quantity = signal.quantity
        if quantity <= 0:
            # Use simple position sizing: risk 1% of capital per trade
            quantity = (self.capital * 0.01) / signal.price if signal.price > 0 else 0

        if quantity <= 0:
            return

        # Check if we have enough capital
        if self.capital < signal.price * quantity:
            return

        # Calculate fill price with slippage
        fill_price = self.fill_simulator.calculate_fill_price(
            signal.price,
            signal.direction == SignalDirection.LONG,
            candle.volume,
        )

        # Create position
        position = Position(
            symbol=signal.symbol,
            exchange=signal.exchange,
            side="long" if signal.direction == SignalDirection.LONG else "short",
            quantity=quantity,
            entry_price=fill_price,
            current_price=fill_price,
            strategy_id=signal.strategy_id,
            entries=[{"price": fill_price, "quantity": quantity, "timestamp": candle.timestamp}],
        )

        # Calculate stop loss and take profit
        if signal.direction == SignalDirection.LONG:
            position.stop_loss = fill_price * (1 - 0.01)  # 1% stop
            position.take_profit = fill_price * (1 + 0.02)  # 2% take profit
        else:
            position.stop_loss = fill_price * (1 + 0.01)
            position.take_profit = fill_price * (1 - 0.02)

        self.positions.append(position)

        # Deduct capital (including commission)
        cost = fill_price * quantity
        commission_cost = cost * self.config.commission
        self.capital -= cost + commission_cost

        self.trades.append(
            {
                "timestamp": candle.timestamp,
                "symbol": signal.symbol,
                "side": signal.direction.value,
                "entry_price": fill_price,
                "quantity": quantity,
                "commission": commission_cost,
                "signal_strength": signal.strength,
            }
        )

    def _close_position(
        self,
        position: Position,
        current_price: float,
        reason: str = "signal",
    ) -> None:
        """Close a position and record PnL."""
        # Calculate fill price with slippage
        fill_price = self.fill_simulator.calculate_fill_price(
            position.avg_entry_price,  # Use entry price as reference
            position.side == "long",
            0,  # Volume not available in position close
        )

        # Calculate PnL
        if position.side == "long":
            pnl = (fill_price - position.avg_entry_price) * position.total_quantity
        else:
            pnl = (position.avg_entry_price - fill_price) * position.total_quantity

        # Apply commission on close
        close_value = fill_price * position.total_quantity
        commission_cost = close_value * self.config.commission
        net_pnl = pnl - commission_cost

        # Update capital
        self.capital += close_value - commission_cost

        # Record trade
        self.trades.append(
            {
                "timestamp": datetime.utcnow(),
                "symbol": position.symbol,
                "side": "close",
                "exit_price": fill_price,
                "quantity": position.total_quantity,
                "pnl": net_pnl,
                "reason": reason,
            }
        )

        # Remove position
        if position in self.positions:
            self.positions.remove(position)

    def _calculate_equity(self, current_price: float) -> float:
        """Calculate total equity including positions."""
        position_value = sum(
            p.current_price * p.total_quantity
            if p.side == "long"
            else (p.avg_entry_price - p.current_price) * p.total_quantity
            for p in self.positions
        )
        return self.capital + position_value

    def run_parameter_sweep(
        self,
        candles: CandleSeries,
        signal_generator: Callable,
        param_ranges: dict,
    ) -> list[BacktestResult]:
        """Run backtest with multiple parameter combinations.

        Args:
            candles: Historical candle data
            signal_generator: Function that generates signals
            param_ranges: Dict of parameter names to value lists

        Returns:
            List of BacktestResult for each parameter combination

        """
        results = []

        # Generate all combinations
        import itertools

        keys = list(param_ranges.keys())
        values = list(param_ranges.values())

        for combination in itertools.product(*values):
            params = dict(zip(keys, combination))

            # Apply parameters (would need to pass to signal generator)
            result = self.run(candles, signal_generator)
            results.append((params, result))

        return results
