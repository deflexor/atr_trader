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
from ..risk.regime_detector import RegimeDetector, MarketRegime, RegimeResult
from ..risk.pre_trade_filter import PreTradeDrawdownFilter, TradeVerdict
from ..risk.boltzmann_sizer import BoltzmannPositionSizer, BoltzmannConfig
from ..risk.bootstrap_stops import BootstrapStopCalculator
from ..risk.drawdown_budget import DrawdownBudgetTracker, DrawdownBudgetConfig

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for backtesting with 1m candle resolution.

    All timing parameters are expressed in 1m candles.
    Position tracking, SL/TP evaluation, and entry/exit all occur
    at 1m candle granularity for precise execution simulation.
    """

    initial_capital: float = 10000.0
    commission: float = 0.0006  # 0.06% per trade (maker+taker avg)
    slippage_type: str = "volume"  # volume or orderbook
    slippage_factor: float = 0.0003  # 0.03% slippage
    max_positions: int = 3
    pyramid_entries: int = 2  # Allow up to N entries per position (pyramiding)
    entry_spacing: float = 0.005  # Price must pullback 0.5% from extreme before 2nd entry
    risk_per_trade: float = 0.015  # 1.5% risk per trade to minimize drawdown
    stop_loss_pct: float = 0.02  # 2% stop loss (fallback when ATR disabled)
    take_profit_pct: float = 0.04  # 4% take profit (fallback, 2:1 R:R)
    use_atr_stops: bool = True  # Use ATR-based dynamic stops
    atr_period: int = 14
    atr_sl_multiplier: float = 99.0  # Disabled — trailing stops only
    atr_tp_multiplier: float = 99.0  # Disabled — trailing stops only
    cooldown_candles: int = 4  # Wait 4 candles (4min on 1m) between trades
    use_trailing_stop: bool = True  # Enable trailing stops after activation
    trailing_activation_atr: float = 2.5  # Activate trailing stop when price moves 2.5*ATR
    trailing_distance_atr: float = 2.5  # Trail at 2.5*ATR behind extreme
    max_drawdown_pct: float = 0.05  # Enable 5% drawdown halt to protect capital
    volatility_adjustment_enabled: bool = True  # Use volatility-adaptive ATR for trailing stops
    volatility_lookback: int = 20  # Lookback for realized volatility calculation
    # Geometric (Kelly) position sizing
    use_geometric_sizing: bool = False  # Kelly criterion sizing (experimental - high drawdown risk)
    kelly_fraction: float = 0.25  # Use 25% of full Kelly for safety
    min_kelly_fraction: float = 0.05  # Minimum position size (5% of capital)

    # Zero-drawdown risk layer
    use_zero_drawdown_layer: bool = True  # Enable regime detection + budget tracking + Boltzmann sizing
    regime_lookback: int = 100  # Lookback for GMM regime feature extraction
    boltzmann_temperature: float = 0.3  # Boltzmann thermal weighting sensitivity
    bootstrap_stops_enabled: bool = True  # Use bootstrap-validated stop distances
    bootstrap_confidence: float = 0.95  # Confidence level for worst-case stop
    bootstrap_simulations: int = 1000  # Number of bootstrap resamples
    bootstrap_horizon: int = 40  # Forward periods to simulate (longer = wider stops)
    bootstrap_min_stop_pct: float = 0.005  # Minimum bootstrap stop (0.5%)
    bootstrap_max_stop_pct: float = 0.10  # Maximum bootstrap stop (10%)
    per_trade_drawdown_budget: float = 0.01  # 1% max drawdown per trade
    total_drawdown_budget: float = 0.03  # 3% total drawdown budget per session


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    initial_capital: float
    final_capital: float
    total_return: float
    total_return_pct: float
    max_drawdown: float
    max_drawdown_vs_initial: float
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
    """Backtesting engine for strategy evaluation with 1m candle resolution.

    Replays historical data through a strategy and calculates
    realistic performance metrics including slippage and commissions.

    Position Tracking (1m Native):
        - Entry/exit signals evaluated on each 1m candle
        - SL/TP checked against candle H/L for realistic fill prices
        - Trailing stops updated after each 1m candle close
        - Equity calculated after each 1m candle for precise drawdown

    Metrics Preserved:
        - Sharpe ratio, max drawdown, win rate
        - All existing performance metrics unchanged
    """

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        fill_simulator: Optional[FillSimulator] = None,
        kelly_sizer=None,  # Optional[KellyPositionSizer] for geometric sizing
    ):
        self.config = config or BacktestConfig()
        self.fill_simulator = fill_simulator or FillSimulator(
            slippage_type=self.config.slippage_type,
            slippage_factor=self.config.slippage_factor,
        )
        self.metrics = PerformanceMetrics()
        self._kelly_sizer = kelly_sizer

        # State
        self.capital = 0.0
        self.positions: list[Position] = []
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self._last_trade_candle: int = -999  # Cooldown tracking
        # Anti-martingale: scale risk by recent win/loss streak
        self._consecutive_losses: int = 0
        self._current_risk_multiplier: float = 1.0  # 1.0=normal, <1 after losses
        self._last_volatility_multiplier: Optional[float] = None  # From last signal

        # Zero-drawdown risk layer
        self._regime_detector: Optional[RegimeDetector] = None
        self._pre_trade_filter: Optional[PreTradeDrawdownFilter] = None
        self._boltzmann_sizer: Optional[BoltzmannPositionSizer] = None
        self._bootstrap_stops: Optional[BootstrapStopCalculator] = None
        self._budget_tracker: Optional[DrawdownBudgetTracker] = None
        self._last_regime_result: Optional[RegimeResult] = None
        self._risk_filter_stats: dict[str, int] = {}  # Diagnostics
        if self.config.use_zero_drawdown_layer:
            self._init_risk_layer()

    def _init_risk_layer(self) -> None:
        """Initialize zero-drawdown risk management components."""
        cfg = self.config
        self._regime_detector = RegimeDetector(lookback=cfg.regime_lookback)
        self._budget_tracker = DrawdownBudgetTracker(
            config=DrawdownBudgetConfig(
                total_budget_pct=cfg.total_drawdown_budget,
                per_trade_budget_pct=cfg.per_trade_drawdown_budget,
            ),
            initial_capital=cfg.initial_capital,
        )
        self._pre_trade_filter = PreTradeDrawdownFilter(
            budget_tracker=self._budget_tracker,
            max_per_trade_dd_pct=cfg.per_trade_drawdown_budget,
        )
        self._boltzmann_sizer = BoltzmannPositionSizer(
            config=BoltzmannConfig(temperature=cfg.boltzmann_temperature),
        )
        if cfg.bootstrap_stops_enabled:
            self._bootstrap_stops = BootstrapStopCalculator(
                confidence_level=cfg.bootstrap_confidence,
                n_simulations=cfg.bootstrap_simulations,
                horizon=cfg.bootstrap_horizon,
                min_stop_pct=cfg.bootstrap_min_stop_pct,
                max_stop_pct=cfg.bootstrap_max_stop_pct,
            )
        self._risk_filter_stats = {
            "regime_rejected": 0,
            "budget_rejected": 0,
            "worst_case_rejected": 0,
            "boltzmann_reduced": 0,
            "vol_spike_reduced": 0,
        }

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
        self._consecutive_losses = 0
        self._current_risk_multiplier = 1.0
        self._drawdown_halted = False
        self._peak_equity = self.config.initial_capital
        self._last_regime_result = None
        # Reset Kelly sizer for new backtest run
        if self._kelly_sizer is not None:
            self._kelly_sizer.reset()
        # Reset risk layer
        if self._budget_tracker is not None:
            self._budget_tracker.reset(initial_capital=self.config.initial_capital)
        if self._regime_detector is not None:
            self._regime_detector.reset()
        if self._bootstrap_stops is not None:
            self._bootstrap_stops.reset()
        for key in self._risk_filter_stats:
            self._risk_filter_stats[key] = 0

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
        kelly_sizer=None,  # Optional[KellyPositionSizer] for geometric sizing
    ) -> BacktestResult:
        """Run backtest on historical data.

        Args:
            candles: Historical candle data
            signal_generator: Function that generates signals from candles
            initial_capital: Override initial capital
            kelly_sizer: Optional KellyPositionSizer for geometric position sizing

        Returns:
            BacktestResult with performance metrics

        """
        self.reset()
        if initial_capital:
            self.capital = initial_capital
        else:
            self.capital = self.config.initial_capital

        # Store Kelly sizer for geometric sizing if provided
        if kelly_sizer is not None:
            self._kelly_sizer = kelly_sizer

        self.start_time = datetime.utcnow()
        logger.info(
            f"Starting backtest: {len(candles.candles)} candles, initial_capital={self.capital}"
        )

        # Process each 1m candle for position tracking and signal evaluation
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
                halted_by_drawdown = self._drawdown_halted
                # Also check budget tracker halt
                if self._budget_tracker is not None and self._budget_tracker.is_halted:
                    halted_by_drawdown = True
                if not in_cooldown and not halted_by_drawdown:
                    # Apply zero-drawdown risk layer if enabled
                    if self.config.use_zero_drawdown_layer and self._last_regime_result is not None:
                        signal = self._apply_risk_layer(signal, candle, visible_candles, i)
                    if signal.direction != SignalDirection.NEUTRAL:
                        self._process_signal(signal, candle, visible_candles)
                        if self.positions and self.positions[-1].strategy_id:
                            self._last_trade_candle = i

            # Record equity
            equity = self._calculate_equity(candle.close)
            self._peak_equity = max(self._peak_equity, equity)

            # Update regime detector with candle return
            if self._regime_detector is not None and i > 0:
                prev_close = candles.candles[i - 1].close
                if prev_close > 0:
                    ret = (candle.close - prev_close) / prev_close
                    self._regime_detector.update(ret)
                    self._last_regime_result = self._regime_detector.detect()

            # Update budget tracker
            if self._budget_tracker is not None:
                self._budget_tracker.update_equity(equity, i)

            # Update bootstrap stops with return
            if self._bootstrap_stops is not None and i > 0:
                prev_close = candles.candles[i - 1].close
                if prev_close > 0:
                    ret = (candle.close - prev_close) / prev_close
                    self._bootstrap_stops.update(ret)

            # Check drawdown halt
            if self.config.max_drawdown_pct > 0 and self._peak_equity > 0:
                drawdown = (self._peak_equity - equity) / self._peak_equity
                if drawdown >= self.config.max_drawdown_pct:
                    self._drawdown_halted = True
                elif equity >= self._peak_equity * (1 - self.config.max_drawdown_pct * 0.5):
                    # Recovered halfway — resume
                    self._drawdown_halted = False

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
        self.metrics.calculate_from_trades(close_trades, self.equity_curve, initial_capital=self.config.initial_capital)

        winning = [t for t in close_trades if t.get("pnl", 0) > 0]
        losing = [t for t in close_trades if t.get("pnl", 0) <= 0]

        # Use only closed trades for win rate calculation
        closed_count = len(close_trades)
        win_rate = len(winning) / closed_count if closed_count else 0

        return BacktestResult(
            initial_capital=self.config.initial_capital,
            final_capital=final_equity,
            total_return=total_return,
            total_return_pct=total_return_pct,
            max_drawdown=self.metrics.max_drawdown,
            max_drawdown_vs_initial=self.metrics.max_drawdown_vs_initial,
            sharpe_ratio=self.metrics.sharpe_ratio,
            win_rate=win_rate,
            total_trades=len(self.trades),  # All trades including entries
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
        """Update positions at 1m candle resolution using candle H/L for SL/TP fills.

        Evaluates on each 1m candle:
            - Stop loss: checked against candle low (longs) or high (shorts)
            - Take profit: checked against candle high (longs) or low (shorts)
            - Trailing stop: updated based on ATR after price update
            - Position price: updated to candle close

        This ensures realistic fill simulation where SL/TP can trigger
        anywhere within the 1m candle's range, not just at close.
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
                    act_atr, dist_atr = self._get_trailing_params()
                    position.update_trailing_stop(act_atr, dist_atr, atr)
                    # Check if trailing stop is now triggered
                    if position.is_trailing_triggered():
                        self._close_position(
                            position, position.trailing_stop, candle.volume, "trailing_stop"
                        )

    # Regime-adaptive trailing stop parameters
    # Only tighten during CRASH — other regimes keep wide trailing for profitability
    _REGIME_TRAILING_MAP: dict[MarketRegime, tuple[float, float]] = {
        MarketRegime.CALM_TRENDING: (8.0, 4.0),      # Wide: let winners run
        MarketRegime.VOLATILE_TRENDING: (6.0, 3.0),   # Slightly tighter
        MarketRegime.MEAN_REVERTING: (8.0, 4.0),      # Keep wide: this is 89% of the time
        MarketRegime.CRASH: (2.0, 1.0),               # Tight: protect capital urgently
    }

    def _get_trailing_params(self) -> tuple[float, float]:
        """Get trailing stop ATR multipliers adapted to current regime.

        Returns (activation_atr, distance_atr).
        Falls back to config defaults if no regime detected.
        """
        if not self._last_regime_result or not self.config.use_zero_drawdown_layer:
            return self.config.trailing_activation_atr, self.config.trailing_distance_atr

        params = self._REGIME_TRAILING_MAP.get(
            self._last_regime_result.regime,
            (self.config.trailing_activation_atr, self.config.trailing_distance_atr),
        )
        return params

    def _apply_risk_layer(
        self,
        signal: Signal,
        candle: Candle,
        visible_candles: Optional[CandleSeries],
        candle_idx: int,
    ) -> Signal:
        """Apply zero-drawdown risk layer to a trading signal.

        Steps:
        1. Attach regime metadata to signal
        2. Pre-trade filter: reject if drawdown budget exceeded or regime is CRASH
        3. Boltzmann sizing: reduce position size by regime confidence
        4. Volatility spike detection: reduce size when vol is spiking

        Returns modified signal (direction may become NEUTRAL if rejected).
        """
        if self._last_regime_result is None:
            return signal

        regime = self._last_regime_result

        # 1. Attach regime metadata
        signal.regime = regime.regime.value

        # 2. Pre-trade drawdown filter
        if self._pre_trade_filter is not None and signal.price > 0:
            atr = self._calculate_atr(visible_candles, self.config.atr_period) if visible_candles else None
            atr_pct = (atr / signal.price) if atr and signal.price > 0 else 0.01
            position_value = self.capital * self.config.risk_per_trade * max(signal.strength, 0.3)

            evaluation = self._pre_trade_filter.evaluate(
                regime_result=regime,
                position_value=position_value,
                capital=self.capital,
                atr_pct=atr_pct,
            )
            signal.risk_verdict = evaluation.verdict.value

            if evaluation.verdict != TradeVerdict.APPROVED:
                stat_key = {
                    TradeVerdict.REJECTED_REGIME: "regime_rejected",
                    TradeVerdict.REJECTED_BUDGET: "budget_rejected",
                    TradeVerdict.REJECTED_WORST_CASE: "worst_case_rejected",
                }.get(evaluation.verdict, "regime_rejected")
                self._risk_filter_stats[stat_key] += 1
                logger.debug(
                    f"Risk filter rejected: {evaluation.verdict.value} "
                    f"regime={regime.regime.value} reason={evaluation.reason}"
                )
                return Signal(
                    symbol=signal.symbol,
                    exchange=signal.exchange,
                    direction=SignalDirection.NEUTRAL,
                    strength=0.0,
                    confidence=0.0,
                    price=signal.price,
                    strategy_id=signal.strategy_id,
                    regime=regime.regime.value,
                    risk_verdict=evaluation.verdict.value,
                )

        # 3. Boltzmann position sizing (regime-based)
        if self._boltzmann_sizer is not None:
            size_fraction = self._boltzmann_sizer.calculate_size_fraction(regime)
            if size_fraction < 1.0:
                signal.quantity *= size_fraction
                self._risk_filter_stats["boltzmann_reduced"] += 1

        # 4. Volatility spike detection: detect rising volatility in real-time
        #    If current ATR is significantly above recent average, reduce position
        if visible_candles and signal.price > 0:
            atr = self._calculate_atr(visible_candles, self.config.atr_period)
            if atr and atr > 0:
                current_atr_pct = atr / signal.price
                # Compare to recent 100-candle average ATR%
                if len(visible_candles.candles) >= 114:
                    hist_atr_pcts = []
                    for i in range(14, min(len(visible_candles.candles), 114)):
                        window = CandleSeries(
                            candles=visible_candles.candles[i-14:i+1],
                            symbol=visible_candles.symbol,
                            exchange=visible_candles.exchange,
                            timeframe=visible_candles.timeframe,
                        )
                        h_atr = self._calculate_atr(window, self.config.atr_period)
                        if h_atr and h_atr > 0:
                            p = visible_candles.candles[i].close
                            if p > 0:
                                hist_atr_pcts.append(h_atr / p)
                    if len(hist_atr_pcts) >= 20:
                        avg_atr_pct = sum(hist_atr_pcts) / len(hist_atr_pcts)
                        vol_ratio = current_atr_pct / avg_atr_pct if avg_atr_pct > 0 else 1.0
                        # If current vol is 2x+ the average, reduce position by half
                        if vol_ratio > 2.0:
                            signal.quantity *= 0.5
                            self._risk_filter_stats["vol_spike_reduced"] = (
                                self._risk_filter_stats.get("vol_spike_reduced", 0) + 1
                            )

        return signal

        regime = self._last_regime_result

        # 1. Attach regime metadata
        signal.regime = regime.regime.value

        # 2. Pre-trade drawdown filter
        if self._pre_trade_filter is not None and signal.price > 0:
            atr = self._calculate_atr(visible_candles, self.config.atr_period) if visible_candles else None
            atr_pct = (atr / signal.price) if atr and signal.price > 0 else 0.01
            position_value = self.capital * self.config.risk_per_trade * max(signal.strength, 0.3)

            evaluation = self._pre_trade_filter.evaluate(
                regime_result=regime,
                position_value=position_value,
                capital=self.capital,
                atr_pct=atr_pct,
            )
            signal.risk_verdict = evaluation.verdict.value

            if evaluation.verdict != TradeVerdict.APPROVED:
                # Neutralize the signal
                stat_key = {
                    TradeVerdict.REJECTED_REGIME: "regime_rejected",
                    TradeVerdict.REJECTED_BUDGET: "budget_rejected",
                    TradeVerdict.REJECTED_WORST_CASE: "worst_case_rejected",
                }.get(evaluation.verdict, "regime_rejected")
                self._risk_filter_stats[stat_key] += 1
                logger.debug(
                    f"Risk filter rejected: {evaluation.verdict.value} "
                    f"regime={regime.regime.value} reason={evaluation.reason}"
                )
                return Signal(
                    symbol=signal.symbol,
                    exchange=signal.exchange,
                    direction=SignalDirection.NEUTRAL,
                    strength=0.0,
                    confidence=0.0,
                    price=signal.price,
                    strategy_id=signal.strategy_id,
                    regime=regime.regime.value,
                    risk_verdict=evaluation.verdict.value,
                )

        # 3. Boltzmann position sizing
        if self._boltzmann_sizer is not None:
            size_fraction = self._boltzmann_sizer.calculate_size_fraction(regime)
            if size_fraction < 1.0:
                signal.quantity *= size_fraction
                self._risk_filter_stats["boltzmann_reduced"] += 1

        # 4. Bootstrap stop override
        if self._bootstrap_stops is not None and len(self._bootstrap_stops._returns) >= 30:
            stop_result = self._bootstrap_stops.calculate()
            if stop_result.stop_distance_pct > 0:
                signal.bootstrap_stop_pct = stop_result.stop_distance_pct
                self._risk_filter_stats["bootstrap_stop_overrides"] += 1

        return signal

    def _process_signal(
        self,
        signal: Signal,
        candle: Candle,
        visible_candles: Optional[CandleSeries] = None,
    ) -> None:
        """Process trading signal and execute orders."""
        is_long = signal.direction == SignalDirection.LONG
        side_str = "long" if is_long else "short"

        # Check if we already have a position in this direction
        existing = next(
            (p for p in self.positions if p.side == side_str),
            None,
        )

        if existing is not None:
            # Pyramid entry: add if we haven't exceeded entry limit
            if len(existing.entries) >= self.config.pyramid_entries:
                return

            # Check entry_spacing pullback: price must have pulled back from extreme
            extreme = existing.highest_price if is_long else existing.lowest_price
            if extreme == 0:
                return
            if is_long:
                retrace_pct = (extreme - signal.price) / extreme
            else:
                retrace_pct = (signal.price - extreme) / extreme

            if retrace_pct < self.config.entry_spacing:
                return  # Not enough pullback for 2nd entry

            # Calculate fill price for pyramid entry
            fill_price = self.fill_simulator.calculate_fill_price(
                signal.price, is_long, candle.volume
            )

            # Same quantity as first entry (don't double up risk)
            quantity = existing.total_quantity

            # Check capital
            required_capital = fill_price * quantity
            if self.capital < required_capital:
                quantity = self.capital * 0.95 / fill_price
                if quantity <= 0:
                    return

            # Add pyramid entry leg
            existing.add_entry(fill_price, quantity)
            existing.update_price(fill_price)

            # Deduct additional capital
            cost = fill_price * quantity
            commission_cost = cost * self.config.commission
            self.capital -= cost + commission_cost

            # Log pyramid entry
            self.trades.append(
                {
                    "timestamp": candle.timestamp,
                    "symbol": signal.symbol,
                    "side": signal.direction.value,
                    "entry_price": fill_price,
                    "quantity": quantity,
                    "commission": commission_cost,
                    "signal_strength": signal.strength,
                    "stop_loss": existing.stop_loss,
                    "take_profit": existing.take_profit,
                    "pyramid_entry": True,
                    "entry_number": len(existing.entries),
                }
            )
            return

        # Check if we can open new position
        if len(self.positions) >= self.config.max_positions:
            return

        # Position sizing: scale by signal strength AND anti-martingale streak
        # Strong signals (ML agrees) → larger position; weak signals → smaller
        # After losses → smaller position; after wins → larger position
        quantity = signal.quantity

        # Use Kelly-based geometric sizing if enabled and sizer available
        if (
            self.config.use_geometric_sizing
            and self._kelly_sizer is not None
            and signal.price > 0
        ):
            kelly_pct = self._kelly_sizer.calculate_kelly_pct()
            position_value = self.capital * kelly_pct
            quantity = position_value / signal.price
        elif quantity <= 0 and signal.price > 0:
            # Fallback to arithmetic sizing
            signal_risk = max(signal.strength, 0.3)
            effective_risk = (
                self.config.risk_per_trade * signal_risk * self._current_risk_multiplier
            )
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
            is_long,
            candle.volume,
        )

        # Calculate stop loss and take profit
        if self.config.use_atr_stops and visible_candles:
            atr = self._calculate_atr(visible_candles, self.config.atr_period)
        else:
            atr = None

        if atr and atr > 0 and self.config.atr_sl_multiplier < 99 and self.config.atr_tp_multiplier < 99:
            sl_distance = atr * self.config.atr_sl_multiplier
            tp_distance = atr * self.config.atr_tp_multiplier
            if is_long:
                stop_loss = fill_price - sl_distance
                take_profit = fill_price + tp_distance
            else:
                stop_loss = fill_price + sl_distance
                take_profit = fill_price - tp_distance
        elif self.config.use_trailing_stop:
            stop_loss = None  # Trailing stop only — no fixed SL
            take_profit = None  # Trailing stop only — no fixed TP
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
            side=side_str,
            quantity=quantity,
            entry_price=fill_price,
            current_price=fill_price,
            strategy_id=signal.strategy_id,
            entries=[{"price": fill_price, "quantity": quantity, "timestamp": candle.timestamp}],
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        # Set volatility-adjusted ATR multiplier for trailing stops if provided
        if (
            self.config.volatility_adjustment_enabled
            and signal.volatility_adjusted_atr_multiplier is not None
            and signal.volatility_adjusted_atr_multiplier > 0
        ):
            position.trailing_atr_multiplier = signal.volatility_adjusted_atr_multiplier
            self._last_volatility_multiplier = signal.volatility_adjusted_atr_multiplier

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
                "pyramid_entry": False,
                "entry_number": 1,
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

        # Record PnL to Kelly sizer for geometric sizing tracking
        if self._kelly_sizer is not None:
            self._kelly_sizer.record_trade(net_pnl)

        # Record PnL to drawdown budget tracker
        if self._budget_tracker is not None:
            self._budget_tracker.record_trade_pnl(net_pnl)

        # Anti-martingale: update streak after close
        if net_pnl > 0:
            self._consecutive_losses = 0
            self._current_risk_multiplier = min(1.5, self._current_risk_multiplier * 1.25)
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= 2:
                self._current_risk_multiplier = max(0.3, self._current_risk_multiplier * 0.5)

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
