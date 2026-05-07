# Trader — Main Async Trading Loop

"""Live trading loop replicating backtest engine position management."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog

from ..core.models.candle import Candle, CandleSeries
from ..core.models.position import Position
from ..core.models.signal import Signal, SignalDirection
from ..execution.exchange_client import ExchangeClient
from ..execution.order_manager import OrderManager
from ..execution.slippage_guard import SlippageGuard
from ..risk.drawdown_budget import DrawdownBudgetConfig, DrawdownBudgetTracker
from ..risk.pre_trade_filter import PreTradeDrawdownFilter
from ..risk.regime_detector import RegimeDetector
from ..strategies.enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal
from .candle_feed import CandleFeed
from .pnl_tracker import PnlTracker
from .state_manager import StateManager

logger = structlog.get_logger(__name__)


@dataclass
class LiveTradingConfig:
    """Live trading configuration."""

    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False
    market_type: str = "perp"
    leverage: int = 1
    symbols: list[str] = field(default_factory=lambda: [
        "BTCUSDT", "ETHUSDT", "DOGEUSDT", "TRXUSDT",
        "SOLUSDT", "ADAUSDT", "AVAXUSDT", "UNIUSDT",
    ])
    initial_capital: float = 10000.0
    commission: float = 0.0006
    risk_per_trade: float = 0.03
    max_positions: int = 2
    timeframe: str = "5m"
    cooldown_candles: int = 96
    max_slippage_pct: float = 0.10
    max_spread_pct: float = 0.15
    limit_order_wait_seconds: int = 30
    use_trailing_stop: bool = True
    trailing_activation_atr: float = 2.0
    trailing_distance_atr: float = 1.5
    use_atr_stops: bool = True
    atr_period: int = 14
    atr_sl_multiplier: float = 2.0
    atr_tp_multiplier: float = 3.0
    lookback_candles: int = 200
    use_zero_drawdown_layer: bool = True
    use_composite_risk: bool = True
    regime_lookback: int = 100
    total_drawdown_budget: float = 0.05
    per_trade_drawdown_budget: float = 0.02


class LiveTrader:
    """Main live trading loop.

    Replicates backtest engine logic for live execution:
    - Position management mirrors BacktestEngine._process_signal
    - Trailing stops mirror BacktestEngine._update_positions_with_candle
    - Risk layer mirrors BacktestEngine._apply_risk_layer
    """

    def __init__(self, config: LiveTradingConfig) -> None:
        self.config = config
        self._running = False
        self._capital = config.initial_capital
        self._positions: dict[str, Position] = {}
        self._anti_martingale_streak = 0
        self._last_signal_candle: dict[str, int] = {}
        self._candle_idx: dict[str, int] = {}
        self._peak_equity = config.initial_capital

        self._exchange_client: Optional[ExchangeClient] = None
        self._state_manager: Optional[StateManager] = None
        self._slippage_guard: Optional[SlippageGuard] = None
        self._order_manager: Optional[OrderManager] = None
        self._candle_feed: Optional[CandleFeed] = None
        self._pnl_tracker: Optional[PnlTracker] = None

        self._regime_detector: Optional[RegimeDetector] = None
        self._budget_tracker: Optional[DrawdownBudgetTracker] = None
        self._pre_trade_filter: Optional[PreTradeDrawdownFilter] = None
        self._signal_config = EnhancedSignalConfig()

    # ── lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize all components and start trading."""
        cfg = self.config
        log = logger.bind(action="start")

        self._state_manager = StateManager()
        await self._state_manager.init_db()

        self._exchange_client = ExchangeClient(
            cfg.api_key, cfg.api_secret,
            testnet=cfg.testnet,
            market_type=cfg.market_type,
            leverage=cfg.leverage,
        )
        await self._exchange_client.start()

        # Configure each symbol for perpetual trading
        if cfg.market_type == "perp":
            for symbol in cfg.symbols:
                try:
                    await self._exchange_client.setup_perp_symbol(symbol)
                except Exception as exc:
                    log.warning("perp_setup_failed", symbol=symbol, error=str(exc))

        self._slippage_guard = SlippageGuard(
            self._exchange_client,
            max_slippage_pct=cfg.max_slippage_pct,
            max_spread_pct=cfg.max_spread_pct,
        )

        self._order_manager = OrderManager(
            exchange_client=self._exchange_client,
            slippage_guard=self._slippage_guard,
            state_manager=self._state_manager,
            limit_order_wait_seconds=cfg.limit_order_wait_seconds,
            commission_pct=cfg.commission,
        )

        self._pnl_tracker = PnlTracker(self._state_manager)

        self._candle_feed = CandleFeed(
            exchange_client=self._exchange_client,
            state_manager=self._state_manager,
            symbols=cfg.symbols,
            timeframe=cfg.timeframe,
            lookback_candles=cfg.lookback_candles,
            market_type=cfg.market_type,
        )
        await self._candle_feed.initialize()

        self._init_risk_layer()

        await self._restore_positions()
        await self._reconcile_positions()

        log.info(
            "live_trader.ready",
            symbols=cfg.symbols,
            capital=self._capital,
            open_positions=len(self._positions),
        )

    async def signal_shutdown(self) -> None:
        """Signal the main loop to exit. Safe to call from any context.

        Does NOT close resources — that's stop()'s job after run() returns.
        """
        self._running = False
        if hasattr(self, "_shutdown_event"):
            self._shutdown_event.set()
        logger.info("live_trader.shutdown_signaled")

    async def stop(self) -> None:
        """Graceful shutdown: save state, close resources.

        Call AFTER run() has returned. The signal handler should call
        signal_shutdown(), not this method.
        """
        self._running = False
        if hasattr(self, "_shutdown_event"):
            self._shutdown_event.set()
        log = logger.bind(action="stop")
        log.info("live_trader.stopping")

        for pos in list(self._positions.values()):
            try:
                await self._state_manager.save_position(pos)
            except Exception as exc:
                log.warning("save_position_failed", symbol=pos.symbol, error=str(exc))

        if self._state_manager:
            await self._state_manager.close()

        if self._exchange_client:
            await self._exchange_client.stop()

        log.info("live_trader.stopped", open_positions=len(self._positions))

    # ── main loop ─────────────────────────────────────────────

    async def run(self) -> None:
        """Main trading loop — process all symbols in parallel."""
        self._running = True
        self._shutdown_event = asyncio.Event()
        log = logger.bind(action="run")
        log.info("live_trader.started", symbols=self.config.symbols)

        while self._running:
            tasks = [
                self._process_symbol(s, stagger=i * 0.2)
                for i, s in enumerate(self.config.symbols)
            ]
            await asyncio.gather(*tasks)
            # Interruptible sleep — wakes immediately on shutdown
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    async def _process_symbol(self, symbol: str, stagger: float = 0.0) -> None:
        """Process one symbol: wait for candle, update, signal."""
        try:
            if stagger > 0:
                await asyncio.sleep(stagger)
            series = await self._candle_feed.wait_for_candle(
                symbol, shutdown_event=getattr(self, "_shutdown_event", None),
            )
            if series is None:
                return

            candle = series.candles[-1]
            self._candle_idx[symbol] = self._candle_idx.get(symbol, 0) + 1

            await self._update_positions(symbol, candle, series)

            signal = generate_enhanced_signal(symbol, series, self._signal_config)
            if signal.is_actionable and signal.direction != SignalDirection.NEUTRAL:
                in_cooldown = self._in_cooldown(symbol)
                if not in_cooldown:
                    await self._process_signal(signal, candle, series)

            positions_list = [
                p for p in self._positions.values() if p.symbol == symbol
            ]
            await self._pnl_tracker.record_equity_snapshot(positions_list, self._capital)

            self._update_regime(candle)

        except Exception as exc:
            logger.warning(
                "symbol_error", symbol=symbol, error=str(exc), exc_info=True,
            )

    # ── core methods mirroring backtest engine ────────────────

    def _calculate_atr(self, candles: CandleSeries, period: int = 14) -> Optional[float]:
        """EXACT replica of BacktestEngine._calculate_atr."""
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

    def _get_trailing_params(self) -> tuple[float, float]:
        """Simplified trailing params — returns config values."""
        return self.config.trailing_activation_atr, self.config.trailing_distance_atr

    async def _process_signal(
        self, signal: Signal, candle: Candle, candle_series: CandleSeries,
    ) -> None:
        """Mirror of BacktestEngine._process_signal."""
        is_long = signal.direction == SignalDirection.LONG
        side_str = "long" if is_long else "short"
        log = logger.bind(
            action="process_signal",
            symbol=signal.symbol,
            direction=side_str,
            strength=signal.strength,
        )

        opposite_side = "short" if is_long else "long"
        opp = next(
            (p for p in self._positions.values() if p.side == opposite_side), None,
        )
        if opp is not None:
            await self._close_position(opp, candle.close, "opposite_signal", candle, candle_series)

        existing = next(
            (p for p in self._positions.values() if p.side == side_str), None,
        )
        if existing is not None:
            return

        symbol_positions = sum(1 for p in self._positions.values() if p.symbol == signal.symbol)
        if symbol_positions >= self.config.max_positions:
            return

        signal_risk = max(signal.strength, 0.3)
        anti_mart_mult = min(1.5, 1.0 + self._anti_martingale_streak * 0.1)
        effective_risk = self.config.risk_per_trade * signal_risk * anti_mart_mult
        position_value = self._capital * effective_risk
        quantity = position_value / signal.price if signal.price > 0 else 0.0

        if quantity <= 0:
            return

        max_affordable = self._capital * 0.95 / signal.price if signal.price > 0 else 0.0
        if quantity > max_affordable:
            quantity = max_affordable
        if quantity <= 0:
            return

        # Build signal context for order debugging
        atr = self._calculate_atr(candle_series, self.config.atr_period)
        signal_context = {
            "signal": {
                "strength": signal.strength,
                "confidence": signal.confidence,
                "direction": signal.direction.value,
                "regime": signal.regime,
                "strategy_id": signal.strategy_id,
            },
            "market": {
                "candle_open": candle.open,
                "candle_high": candle.high,
                "candle_low": candle.low,
                "candle_close": candle.close,
                "candle_volume": candle.volume,
                "atr": atr,
            },
            "sizing": {
                "risk_per_trade": self.config.risk_per_trade,
                "signal_risk": signal_risk,
                "anti_martingale_streak": self._anti_martingale_streak,
                "anti_martingale_mult": anti_mart_mult,
                "effective_risk": effective_risk,
                "capital": self._capital,
            },
        }

        side = "buy" if is_long else "sell"
        result = await self._order_manager.place_entry_order(
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            signal_price=signal.price,
            reason="signal",
            signal_context=signal_context,
        )

        if result.status not in ("filled", "partial") or result.fill_price is None:
            log.warning("entry_not_filled", status=result.status)
            return

        fill_price = result.fill_price
        filled_qty = result.filled_quantity
        if filled_qty <= 0:
            return

        stop_loss: Optional[float] = None
        take_profit: Optional[float] = None
        if self.config.use_atr_stops:
            if atr and atr > 0:
                if is_long:
                    stop_loss = fill_price - atr * self.config.atr_sl_multiplier
                    take_profit = fill_price + atr * self.config.atr_tp_multiplier
                else:
                    stop_loss = fill_price + atr * self.config.atr_sl_multiplier
                    take_profit = fill_price - atr * self.config.atr_tp_multiplier

        pos = Position(
            id=str(uuid.uuid4()),
            symbol=signal.symbol,
            exchange="bybit",
            side=side_str,
            current_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_id="enhanced",
            highest_price=fill_price if is_long else 0.0,
            lowest_price=fill_price if not is_long else float("inf"),
        )
        pos.add_entry(fill_price, filled_qty)

        self._positions[pos.id] = pos
        self._last_signal_candle[signal.symbol] = self._candle_idx.get(signal.symbol, 0)

        entry_cost = fill_price * filled_qty
        commission_cost = entry_cost * self.config.commission
        self._capital -= entry_cost + commission_cost

        await self._state_manager.save_position(pos)

        log.info(
            "position_opened",
            position_id=pos.id,
            fill_price=fill_price,
            quantity=filled_qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            commission=commission_cost,
        )

    async def _update_positions(
        self, symbol: str, candle: Candle, candle_series: CandleSeries,
    ) -> None:
        """Mirror of BacktestEngine._update_positions_with_candle."""
        for pos in list(self._positions.values()):
            if pos.symbol != symbol:
                continue

            closed = False

            if pos.side == "long":
                if pos.stop_loss and candle.low <= pos.stop_loss:
                    pos.update_price(pos.stop_loss)
                    await self._close_position(pos, pos.stop_loss, "stop_loss", candle, candle_series)
                    closed = True
                elif pos.take_profit and candle.high >= pos.take_profit:
                    pos.update_price(pos.take_profit)
                    await self._close_position(pos, pos.take_profit, "take_profit", candle, candle_series)
                    closed = True
            else:
                if pos.stop_loss and candle.high >= pos.stop_loss:
                    pos.update_price(pos.stop_loss)
                    await self._close_position(pos, pos.stop_loss, "stop_loss", candle, candle_series)
                    closed = True
                elif pos.take_profit and candle.low <= pos.take_profit:
                    pos.update_price(pos.take_profit)
                    await self._close_position(pos, pos.take_profit, "take_profit", candle, candle_series)
                    closed = True

            if closed:
                continue

            pos.update_price(candle.close)

            if self.config.use_trailing_stop:
                atr = self._calculate_atr(candle_series, self.config.atr_period)
                if atr and atr > 0:
                    act_atr, dist_atr = self._get_trailing_params()
                    pos.update_trailing_stop(act_atr, dist_atr, atr)
                    if pos.is_trailing_triggered():
                        await self._close_position(
                            pos, pos.trailing_stop, "trailing_stop",
                            candle, candle_series,
                        )

    async def _close_position(
        self,
        position: Position,
        exit_price: float,
        reason: str,
        candle: Optional[Candle] = None,
        candle_series: Optional[CandleSeries] = None,
    ) -> None:
        """Mirror of BacktestEngine._close_position."""
        log = logger.bind(
            action="close_position",
            symbol=position.symbol,
            side=position.side,
            reason=reason,
        )

        exit_side = "sell" if position.side == "long" else "buy"
        result = await self._order_manager.place_exit_order(
            symbol=position.symbol,
            side=exit_side,
            quantity=position.total_quantity,
            reason=reason,
            position_id=position.id,
        )

        fill = exit_price
        if result.status in ("filled", "partial") and result.fill_price:
            fill = result.fill_price

        if position.side == "long":
            pnl = (fill - position.avg_entry_price) * position.total_quantity
        else:
            pnl = (position.avg_entry_price - fill) * position.total_quantity

        close_value = fill * position.total_quantity
        commission_cost = close_value * self.config.commission
        net_pnl = pnl - commission_cost

        entry_value = position.avg_entry_price * position.total_quantity
        self._capital += entry_value + net_pnl

        if net_pnl > 0:
            self._anti_martingale_streak += 1
        else:
            self._anti_martingale_streak = 0

        # Build market context for trade debugging
        atr = self._calculate_atr(candle_series, self.config.atr_period) if candle_series else None
        market_context: dict = {
            "anti_martingale_streak": self._anti_martingale_streak,
            "capital_after": self._capital,
        }
        if candle:
            market_context["exit_candle"] = {
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "timestamp": candle.timestamp.isoformat() if hasattr(candle.timestamp, "isoformat") else str(candle.timestamp),
            }
        if atr:
            market_context["atr_at_exit"] = atr

        await self._pnl_tracker.record_trade_closed(
            position=position,
            exit_price=fill,
            exit_reason=reason,
            commission=commission_cost,
            slippage=abs(fill - exit_price) * position.total_quantity if exit_price else 0.0,
            market_context=market_context,
        )

        await self._state_manager.mark_position_closed(position.id)
        self._positions.pop(position.id, None)

        log.info(
            "position_closed",
            position_id=position.id,
            fill=fill,
            net_pnl=net_pnl,
            commission=commission_cost,
            streak=self._anti_martingale_streak,
        )

    # ── helpers ───────────────────────────────────────────────

    def _calculate_equity(self) -> float:
        """Calculate total equity including positions."""
        pos_value = sum(
            p.current_price * p.total_quantity
            if p.side == "long"
            else (p.avg_entry_price - p.current_price) * p.total_quantity
            for p in self._positions.values()
        )
        return self._capital + pos_value

    def _in_cooldown(self, symbol: str) -> bool:
        """Check if symbol is in cooldown period."""
        last = self._last_signal_candle.get(symbol, -999)
        current = self._candle_idx.get(symbol, 0)
        return (current - last) < self.config.cooldown_candles

    def _init_risk_layer(self) -> None:
        """Initialize risk management components."""
        cfg = self.config
        if cfg.use_zero_drawdown_layer:
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

    def _update_regime(self, candle: Candle) -> None:
        """Update regime detector with latest candle return."""
        if self._regime_detector is not None:
            self._regime_detector.update(0.0)
        equity = self._calculate_equity()
        self._peak_equity = max(self._peak_equity, equity)
        if self._budget_tracker is not None:
            self._budget_tracker.update_equity(equity, 0)

    async def _restore_positions(self) -> None:
        """Restore open positions from state manager."""
        if self._state_manager is None:
            return
        try:
            open_positions = await self._state_manager.load_open_positions()
            for pos in open_positions:
                self._positions[pos.id] = pos
                self._capital -= pos.cost_basis
            if open_positions:
                logger.info(
                    "positions_restored",
                    count=len(open_positions),
                    symbols=[p.symbol for p in open_positions],
                )
        except Exception as exc:
            logger.warning("restore_positions_failed", error=str(exc))

    async def _reconcile_positions(self) -> None:
        """Reconcile local state with exchange positions.

        Imports orphan positions from Bybit that aren't in the local DB.
        This handles cases where orders were placed but fill-polling failed
        (e.g. the fetchOrder 500-limit error).
        """
        if self._exchange_client is None or self._state_manager is None:
            return
        if self.config.market_type != "perp":
            return

        try:
            exchange_positions = await self._exchange_client.fetch_exchange_positions(
                self.config.symbols
            )
            if not exchange_positions:
                return

            known_symbols = {
                p.symbol for p in self._positions.values()
            }

            imported = []
            for ep in exchange_positions:
                if ep["symbol"] in known_symbols:
                    continue
                # Skip if a local position already tracks this symbol
                # (could be different side — both would be new)

                pos = Position(
                    id=str(uuid.uuid4()),
                    symbol=ep["symbol"],
                    exchange="bybit",
                    side=ep["side"],
                    current_price=ep["entry_price"],
                    highest_price=ep["entry_price"] if ep["side"] == "long" else 0.0,
                    lowest_price=ep["entry_price"] if ep["side"] == "short" else float("inf"),
                    strategy_id="reconciled",
                )
                pos.add_entry(ep["entry_price"], ep["quantity"])

                self._positions[pos.id] = pos
                self._capital -= pos.cost_basis
                known_symbols.add(ep["symbol"])

                await self._state_manager.save_position(pos)
                imported.append(ep)

            if imported:
                logger.warning(
                    "positions_reconciled",
                    count=len(imported),
                    symbols=[f"{p['symbol']}:{p['side']}" for p in imported],
                    note="orphaned positions imported from exchange",
                )
        except Exception as exc:
            logger.warning("reconcile_positions_failed", error=str(exc))
