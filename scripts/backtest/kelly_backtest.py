"""Kelly Criterion Position Sizing Backtest

Compares fixed 1.5% risk vs Kelly-based position sizing on 7-day backtest.
Kelly formula: f* = (b × p - q) / b

Usage: uv run python scripts/backtest/kelly_backtest.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.core.models.signal import Signal, SignalDirection
from src.core.models.position import Position
from src.backtest.engine import BacktestEngine, BacktestConfig, BacktestResult
from src.strategies.momentum_strategy import (
    MomentumStrategy,
    MomentumConfig,
    KellyPositionSizer,
)
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class BacktestMetrics:
    """Standard metrics for backtest comparison."""
    total_trades: int
    win_rate: float
    max_drawdown: float
    sharpe_ratio: float
    total_return_pct: float
    avg_win: float
    avg_loss: float
    final_capital: float
    kelly_pct: float = 0.0


async def fetch_candles(symbol: str, days: int) -> CandleSeries:
    """Fetch 1m candles from Bybit."""
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)

    candles = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles.append(Candle(
                symbol=symbol,
                exchange="bybit",
                timeframe="1m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            ))
        except Exception:
            continue

    candles.sort(key=lambda x: x.timestamp.timestamp())
    seen = {}
    result = []
    for c in candles:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)

    return CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1m")


class KellyBacktestEngine(BacktestEngine):
    """Backtest engine with Kelly position sizing support.

    Records trade PnL for Kelly calculation and uses Kelly-based sizing.
    """

    def __init__(self, config: BacktestConfig, kelly_sizer: KellyPositionSizer = None):
        super().__init__(config)
        self.kelly_sizer = kelly_sizer or KellyPositionSizer()
        self._fixed_sizing = config.risk_per_trade

    def reset(self) -> None:
        """Reset backtest state and Kelly tracker."""
        super().reset()
        self.kelly_sizer.reset()

    def _record_pnl(self, pnl: float) -> None:
        """Record closed trade PnL for Kelly calculation."""
        self.kelly_sizer.record_trade(pnl)

    def _process_signal_with_kelly(
        self,
        signal: Signal,
        candle: Candle,
        visible_candles,
    ) -> None:
        """Process signal with Kelly-based position sizing."""
        is_long = signal.direction == SignalDirection.LONG
        side_str = "long" if is_long else "short"

        existing = next((p for p in self.positions if p.side == side_str), None)
        if existing is not None:
            return  # Pyramid not implemented for Kelly variant

        if len(self.positions) >= self.config.max_positions:
            return

        # Use Kelly-based sizing (signal.strength scales the Kelly %)
        kelly_pct = self.kelly_sizer.calculate_kelly_pct()
        effective_risk = kelly_pct * signal.strength if signal.strength > 0 else kelly_pct

        position_value = self.capital * effective_risk
        quantity = position_value / signal.price if signal.price > 0 else 0

        if quantity <= 0:
            return

        required_capital = signal.price * quantity
        if self.capital < required_capital:
            quantity = self.capital * 0.95 / signal.price
            if quantity <= 0:
                return

        fill_price = self.fill_simulator.calculate_fill_price(
            signal.price, is_long, candle.volume
        )

        # Use trailing stop only
        stop_loss = None
        take_profit = None

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

        self.positions.append(position)

        cost = fill_price * quantity
        commission_cost = cost * self.config.commission
        self.capital -= cost + commission_cost

        self.trades.append({
            "timestamp": candle.timestamp,
            "symbol": signal.symbol,
            "side": signal.direction.value,
            "entry_price": fill_price,
            "quantity": quantity,
            "commission": commission_cost,
            "signal_strength": signal.strength,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "kelly_pct": kelly_pct,
            "pyramid_entry": False,
            "entry_number": 1,
        })

    async def run(
        self,
        candles: CandleSeries,
        signal_generator,
        initial_capital: float = None,
        use_kelly: bool = True,
    ) -> BacktestResult:
        """Run backtest with optional Kelly sizing.

        Args:
            candles: Historical candle data
            signal_generator: Function that generates signals
            initial_capital: Override initial capital
            use_kelly: If True, use Kelly sizing; if False, use fixed risk_per_trade
        """
        self.reset()
        self.capital = initial_capital or self.config.initial_capital

        self.start_time = datetime.utcnow()
        logger.info(
            f"Starting backtest: {len(candles.candles)} candles, "
            f"initial_capital={self.capital}, use_kelly={use_kelly}"
        )

        for i, candle in enumerate(candles.candles):
            timestamp = candle.timestamp

            visible_candles = CandleSeries(
                candles=candles.candles[: i + 1],
                symbol=candles.symbol,
                exchange=candles.exchange,
                timeframe=candles.timeframe,
            )

            self._update_positions_with_candle(candle, visible_candles, i)

            signal = await signal_generator(candles.symbol, visible_candles)

            if signal.is_actionable and signal.direction != SignalDirection.NEUTRAL:
                in_cooldown = (i - self._last_trade_candle) < self.config.cooldown_candles
                halted_by_drawdown = self._drawdown_halted
                if not in_cooldown and not halted_by_drawdown:
                    if use_kelly:
                        self._process_signal_with_kelly(signal, candle, visible_candles)
                    else:
                        self._process_signal(signal, candle, visible_candles)
                    if self.positions and self.positions[-1].strategy_id:
                        self._last_trade_candle = i

            equity = self._calculate_equity(candle.close)
            self._peak_equity = max(self._peak_equity, equity)

            if self.config.max_drawdown_pct > 0 and self._peak_equity > 0:
                drawdown = (self._peak_equity - equity) / self._peak_equity
                if drawdown >= self.config.max_drawdown_pct:
                    self._drawdown_halted = True
                elif equity >= self._peak_equity * (1 - self.config.max_drawdown_pct * 0.5):
                    self._drawdown_halted = False

            self.equity_curve.append({
                "timestamp": timestamp,
                "equity": equity,
                "positions": len(self.positions),
            })

        self.end_time = datetime.utcnow()
        duration = (self.end_time - self.start_time).total_seconds()

        final_equity = self._calculate_equity(
            candles.candles[-1].close if candles.candles else 0
        )
        total_return = final_equity - self.config.initial_capital
        total_return_pct = (total_return / self.config.initial_capital) * 100

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


def print_comparison(fixed: BacktestMetrics, kelly: BacktestMetrics) -> None:
    """Print side-by-side comparison table."""
    header = f"{'Metric':<20} {'Fixed 1.5%':>15} {'Kelly':>15} {'Diff':>10}"
    print("\n" + "=" * 65)
    print("KELLY CRITERION vs FIXED SIZING COMPARISON")
    print("=" * 65)
    print(header)
    print("-" * 65)

    metrics = [
        ("Total Trades", fixed.total_trades, kelly.total_trades, None),
        ("Win Rate", fixed.win_rate * 100, kelly.win_rate * 100, "%"),
        ("Max Drawdown", fixed.max_drawdown, kelly.max_drawdown, "%"),
        ("Sharpe Ratio", fixed.sharpe_ratio, kelly.sharpe_ratio, None),
        ("Return %", fixed.total_return_pct, kelly.total_return_pct, "%"),
        ("Avg Win", fixed.avg_win, kelly.avg_win, "$"),
        ("Avg Loss", fixed.avg_loss, kelly.avg_loss, "$"),
        ("Final Capital", fixed.final_capital, kelly.final_capital, "$"),
    ]

    for name, fixed_val, kelly_val, unit in metrics:
        if unit == "$":
            diff = kelly_val - fixed_val
            diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
            print(f"{name:<20} {fixed_val:>15.2f} {kelly_val:>15.2f} {diff_str:>10}")
        elif unit == "%":
            diff = kelly_val - fixed_val
            diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
            print(f"{name:<20} {fixed_val:>14.1f}% {kelly_val:>14.1f}% {diff_str:>10}")
        else:
            diff = kelly_val - fixed_val
            diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
            print(f"{name:<20} {fixed_val:>15.2f} {kelly_val:>15.2f} {diff_str:>10}")

    print("-" * 65)
    # Highlight Kelly advantage
    if kelly.sharpe_ratio > fixed.sharpe_ratio:
        print("✓ Kelly sizing outperforms fixed by Sharpe")
    else:
        print("✗ Fixed sizing outperforms Kelly by Sharpe")


def save_results_md(fixed: BacktestMetrics, kelly: BacktestMetrics, date_str: str) -> Path:
    """Save results to markdown file."""
    filepath = Path(f"results/kelly_criterion_{date_str}.md")
    filepath.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Kelly Criterion Position Sizing Results",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat()}",
        "\n## Configuration",
        "- Symbol: BTCUSDT",
        "- Backtest Period: 7 days",
        "- Initial Capital: 10,000 USDT",
        "- Kelly Cap: 10% max position",
        "\n## Comparison",
        "\n| Metric | Fixed 1.5% | Kelly | Difference |",
        "|---|---|---|---|",
        f"| Total Trades | {fixed.total_trades} | {kelly.total_trades} | - |",
        f"| Win Rate | {fixed.win_rate*100:.1f}% | {kelly.win_rate*100:.1f}% | {((kelly.win_rate - fixed.win_rate)*100):+.1f}% |",
        f"| Max Drawdown | {fixed.max_drawdown:.2f}% | {kelly.max_drawdown:.2f}% | {kelly.max_drawdown - fixed.max_drawdown:+.2f}% |",
        f"| Sharpe Ratio | {fixed.sharpe_ratio:.2f} | {kelly.sharpe_ratio:.2f} | {kelly.sharpe_ratio - fixed.sharpe_ratio:+.2f} |",
        f"| Return | {fixed.total_return_pct:.2f}% | {kelly.total_return_pct:.2f}% | {kelly.total_return_pct - fixed.total_return_pct:+.2f}% |",
        f"| Final Capital | ${fixed.final_capital:.2f} | ${kelly.final_capital:.2f} | ${kelly.final_capital - fixed.final_capital:+.2f} |",
        "\n## Kelly Details",
        f"- Average Kelly % used: {kelly.kelly_pct*100:.1f}%",
        f"- Kelly formula: f* = (b × p - q) / b",
    ]

    filepath.write_text("\n".join(lines))
    return filepath


async def main():
    symbol = "BTCUSDT"
    days = 7
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 1. Fetch candles
    print(f"Fetching {days} days of 1m candles from Bybit...")
    candles_1m = await fetch_candles(symbol, days)
    print(f"Got {len(candles_1m.candles)} 1m candles")

    if len(candles_1m.candles) < 1000:
        print(f"WARNING: Only {len(candles_1m.candles)} candles - may be insufficient")

    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")
    else:
        print("WARNING: H1Model not found - using momentum strategy only")

    # 3. Setup strategy
    config_h1 = MomentumConfig(
        name="h1_test",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config_h1, h1_model=h1_model)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    # 4. Run backtest with FIXED sizing
    print("\nRunning backtest with FIXED 1.5% risk...")
    kelly_sizer = KellyPositionSizer()
    config_fixed = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        use_trailing_stop=True,
        trailing_activation_atr=2.5,
        trailing_distance_atr=2.5,
        use_atr_stops=False,
        atr_sl_multiplier=99.0,
        atr_tp_multiplier=99.0,
    )

    engine_fixed = KellyBacktestEngine(config_fixed, kelly_sizer)
    result_fixed = await engine_fixed.run(candles_1m, signal_gen, 10000.0, use_kelly=False)

    fixed_metrics = BacktestMetrics(
        total_trades=result_fixed.total_trades,
        win_rate=result_fixed.win_rate,
        max_drawdown=result_fixed.max_drawdown,
        sharpe_ratio=result_fixed.sharpe_ratio,
        total_return_pct=result_fixed.total_return_pct,
        avg_win=result_fixed.avg_win,
        avg_loss=result_fixed.avg_loss,
        final_capital=result_fixed.final_capital,
        kelly_pct=0.015,  # Fixed at 1.5%
    )

    print(f"  Trades: {fixed_metrics.total_trades}, Sharpe: {fixed_metrics.sharpe_ratio:.2f}")

    # 5. Run backtest with KELLY sizing
    print("\nRunning backtest with KELLY-based sizing...")
    kelly_sizer_fresh = KellyPositionSizer(max_kelly_pct=0.10)
    config_kelly = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        use_trailing_stop=True,
        trailing_activation_atr=2.5,
        trailing_distance_atr=2.5,
        use_atr_stops=False,
        atr_sl_multiplier=99.0,
        atr_tp_multiplier=99.0,
    )

    engine_kelly = KellyBacktestEngine(config_kelly, kelly_sizer_fresh)
    result_kelly = await engine_kelly.run(candles_1m, signal_gen, 10000.0, use_kelly=True)

    kelly_pct_used = kelly_sizer_fresh.calculate_kelly_pct()
    kelly_metrics = BacktestMetrics(
        total_trades=result_kelly.total_trades,
        win_rate=result_kelly.win_rate,
        max_drawdown=result_kelly.max_drawdown,
        sharpe_ratio=result_kelly.sharpe_ratio,
        total_return_pct=result_kelly.total_return_pct,
        avg_win=result_kelly.avg_win,
        avg_loss=result_kelly.avg_loss,
        final_capital=result_kelly.final_capital,
        kelly_pct=kelly_pct_used,
    )

    print(f"  Trades: {kelly_metrics.total_trades}, Sharpe: {kelly_metrics.sharpe_ratio:.2f}, Kelly%: {kelly_pct_used*100:.1f}%")

    # 6. Print comparison
    print_comparison(fixed_metrics, kelly_metrics)

    # 7. Save results
    filepath = save_results_md(fixed_metrics, kelly_metrics, date_str)
    print(f"\nResults saved to: {filepath}")


if __name__ == "__main__":
    asyncio.run(main())