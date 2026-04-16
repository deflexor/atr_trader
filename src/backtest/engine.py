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
    commission: float = 0.0006  # 0.06% per trade (maker+taker avg)
    slippage_type: str = "volume"  # volume or orderbook
    slippage_factor: float = 0.0003  # 0.03% slippage
    max_positions: int = 3
    risk_per_trade: float = 0.02  # 2% of capital per trade
    stop_loss_pct: float = 0.02  # 2% stop loss (fallback)
    take_profit_pct: float = 0.04  # 4% take profit (fallback, 2:1 R:R)
    use_atr_stops: bool = True  # Use ATR-based dynamic stops
    atr_period: int = 14
    atr_sl_multiplier: float = 3.0  # SL = 3x ATR (wide enough to avoid noise)
    atr_tp_multiplier: float = 4.0  # TP = 4x ATR (1.33:1 R:R, reachable vs old 6x)
    cooldown_candles: int = 4  # Wait 4 candles (20min on 5m) between trades
    use_trailing_stop: bool = True  # Enable trailing stops after activation
    trailing_activation_atr: float = 2.0  # Activate after 2x ATR profit (let trend develop)
    trailing_distance_atr: float = 2.0  # Trail at 2x ATR behind extreme (room to breathe)


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
        self._last_trade_candle: int = -999  # Cooldown tracking

    def reset(self) -> None:
        """Reset backtest state for new run."""
        self.capital = self.config.initial_capital
        self.positions = []
        self.trades = []
        self.equity_curve = []
        self.start_time = None
        self.end_time = None
        self._last_trade_candle = -999
        self.metrics.reset()

    def _calculate_atr(self, candles: CandleSeries, period: int = 14) -> Optional[float]:
        """Calculate Average True Range for dynamic stop sizing."""
        if len(candles.candles) < period + 1:
            return None

        true_ranges = []
        for i in range(-period, 0):
            c = candles.candles[i]
            prev_c = candles.candles[i - 1]
            tr = max(
                c.high - c.low,
                abs(c.high - prev_c.close),
                abs(c.low - prev_c.close),
            )
            true_ranges.append(tr)

        return sum(true_ranges) / len(true_ranges) if true_ranges else None

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

            # Build a view of candles up to current point (strategy sees only past)
            visible_candles = CandleSeries(
                candles=candles.candles[: i + 1],
                symbol=candles.symbol,
                exchange=candles.exchange,
                timeframe=candles.timeframe,
            )

            # Update positions with candle high/low for realistic SL/TP
            self._update_positions_with_candle(candle, visible_candles, i)

            # Generate signal using only visible data
            signal = await signal_generator(candles.symbol, visible_candles)

            # Process signal: open new position if cooldown passed
            if signal.is_actionable and signal.direction != SignalDirection.NEUTRAL:
                in_cooldown = (i - self._last_trade_candle) < self.config.cooldown_candles
                if not in_cooldown:
                    self._process_signal(signal, candle, visible_candles)
                    if self.positions and self.positions[-1].strategy_id:
                        self._last_trade_candle = i

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

        # Compute performance metrics (Sharpe, drawdown, etc.) from trades + equity curve
        close_trades = [t for t in self.trades if t.get("pnl") is not None]
        self.metrics.calculate_from_trades(close_trades, self.equity_curve)

        winning = [t for t in close_trades if t.get("pnl", 0) > 0]
        losing = [t for t in close_trades if t.get("pnl", 0) <= 0]

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
                self._close_position(position, current_price, 0, "stop_or_tp")

    def _update_positions_with_candle(
        self,
        candle: Candle,
        visible_candles: CandleSeries,
        candle_idx: int,
    ) -> None:
        """Update positions using candle H/L for realistic SL/TP fills.

        Checks if the candle's high or low would trigger stops before
        processing the close price.
        """
        for position in self.positions[:]:
            # Check stop loss using worst-case price within candle
            if position.side == "long":
                # For longs, check if low hits stop loss
                if position.stop_loss and candle.low <= position.stop_loss:
                    position.update_price(position.stop_loss)
                    self._close_position(position, position.stop_loss, candle.volume, "stop_loss")
                    continue
                # Check if high hits take profit
                if position.take_profit and candle.high >= position.take_profit:
                    position.update_price(position.take_profit)
                    self._close_position(
                        position, position.take_profit, candle.volume, "take_profit"
                    )
                    continue
            else:
                # For shorts, check if high hits stop loss
                if position.stop_loss and candle.high >= position.stop_loss:
                    position.update_price(position.stop_loss)
                    self._close_position(position, position.stop_loss, candle.volume, "stop_loss")
                    continue
                # Check if low hits take profit
                if position.take_profit and candle.low <= position.take_profit:
                    position.update_price(position.take_profit)
                    self._close_position(
                        position, position.take_profit, candle.volume, "take_profit"
                    )
                    continue

            # No trigger — update to close price
            position.update_price(candle.close)

            # Update trailing stop after price update
            if self.config.use_trailing_stop:
                atr = self._calculate_atr(visible_candles, self.config.atr_period)
                if atr and atr > 0:
                    position.update_trailing_stop(
                        self.config.trailing_activation_atr,
                        self.config.trailing_distance_atr,
                        atr,
                    )
                    # Check if trailing stop is now triggered
                    if position.is_trailing_triggered():
                        self._close_position(
                            position, position.trailing_stop, candle.volume, "trailing_stop"
                        )

    def _process_signal(
        self,
        signal: Signal,
        candle: Candle,
        visible_candles: Optional[CandleSeries] = None,
    ) -> None:
        """Process trading signal and execute orders."""
        # Don't open if we already have a position in this direction
        same_direction = any(
            (p.side == "long" and signal.direction == SignalDirection.LONG)
            or (p.side == "short" and signal.direction == SignalDirection.SHORT)
            for p in self.positions
        )
        if same_direction:
            return

        # Check if we can open new position
        if len(self.positions) >= self.config.max_positions:
            return

        # Position sizing: scale risk by signal strength/confidence
        # Strong signals (ML agrees) → larger position; weak signals → smaller
        quantity = signal.quantity
        if quantity <= 0 and signal.price > 0:
            # Scale risk by combined strength to size up on high-confidence entries
            effective_risk = self.config.risk_per_trade * max(signal.strength, 0.3)
            position_value = self.capital * effective_risk
            quantity = position_value / signal.price

        if quantity <= 0:
            return

        # Check if we have enough capital for the position
        required_capital = signal.price * quantity
        if self.capital < required_capital:
            # Scale down to what we can afford
            quantity = self.capital * 0.95 / signal.price  # Leave 5% buffer
            if quantity <= 0:
                return

        # Calculate fill price with slippage
        fill_price = self.fill_simulator.calculate_fill_price(
            signal.price,
            signal.direction == SignalDirection.LONG,
            candle.volume,
        )

        # Calculate stop loss and take profit
        if self.config.use_atr_stops and visible_candles:
            atr = self._calculate_atr(visible_candles, self.config.atr_period)
        else:
            atr = None

        is_long = signal.direction == SignalDirection.LONG

        if atr and atr > 0:
            sl_distance = atr * self.config.atr_sl_multiplier
            tp_distance = atr * self.config.atr_tp_multiplier
        else:
            sl_distance = fill_price * self.config.stop_loss_pct
            tp_distance = fill_price * self.config.take_profit_pct

        if is_long:
            stop_loss = fill_price - sl_distance
            take_profit = fill_price + tp_distance
        else:
            stop_loss = fill_price + sl_distance
            take_profit = fill_price - tp_distance

        # Create position
        position = Position(
            symbol=signal.symbol,
            exchange=signal.exchange,
            side="long" if is_long else "short",
            quantity=quantity,
            entry_price=fill_price,
            current_price=fill_price,
            strategy_id=signal.strategy_id,
            entries=[{"price": fill_price, "quantity": quantity, "timestamp": candle.timestamp}],
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

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
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )

    def _close_position(
        self,
        position: Position,
        current_price: float,
        volume: float = 0,
        reason: str = "signal",
    ) -> None:
        """Close a position and record PnL.

        Args:
            position: Position to close
            current_price: Current market price (NOT entry price!)
            volume: Candle volume for slippage calc
            reason: Why the position was closed
        """
        # Calculate fill price with slippage based on CURRENT price
        # For closing longs we sell (is_buy=False), for closing shorts we buy (is_buy=True)
        fill_price = self.fill_simulator.calculate_fill_price(
            current_price,  # FIXED: was using entry_price!
            position.side != "long",  # Closing: reverse the buy direction
            volume,
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

        # Update capital: return the position's value minus commission
        entry_value = position.avg_entry_price * position.total_quantity
        self.capital += entry_value + net_pnl

        # Record trade
        self.trades.append(
            {
                "timestamp": datetime.utcnow(),
                "symbol": position.symbol,
                "side": "close",
                "entry_price": position.avg_entry_price,
                "exit_price": fill_price,
                "quantity": position.total_quantity,
                "pnl": net_pnl,
                "pnl_pct": (net_pnl / entry_value * 100) if entry_value > 0 else 0,
                "reason": reason,
                "commission": commission_cost,
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
