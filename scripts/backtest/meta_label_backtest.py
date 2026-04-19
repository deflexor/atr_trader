"""Meta-Labeling Backtest Script

Compares strategy performance with and without meta-labeling filtering.
Meta-labeling filters out low-quality signals using a secondary ML model.

Usage: uv run python scripts/backtest/meta_label_backtest.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Callable

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.core.models.signal import Signal, SignalDirection
from src.backtest.engine import BacktestEngine, BacktestConfig, BacktestResult
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model
from src.ml.meta_label_model import (
    MetaLabelModel,
    MetaLabelConfig,
    TradeFeatures,
    LabeledTrade,
    extract_features_from_trade,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


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


class MetaLabelBacktestEngine(BacktestEngine):
    """Backtest engine that captures trade features for meta-labeling."""

    def __init__(self, config: BacktestConfig, meta_model: MetaLabelModel = None):
        super().__init__(config)
        self.meta_model = meta_model
        self.use_meta_label = meta_model is not None
        self.trade_features: List[dict] = []

    def _extract_trade_features(
        self,
        signal: Signal,
        mtf_info: dict,
        candles: CandleSeries,
    ) -> TradeFeatures:
        """Extract features from signal for meta-labeling."""
        # Calculate ATR %
        atr = self._calculate_atr(candles, 14)
        current_price = signal.price if signal.price > 0 else candles.candles[-1].close
        atr_pct = atr / current_price if current_price > 0 else 0.0

        # Calculate volume spike
        vol_spike = 1.0
        if len(candles.candles) >= 20:
            recent_vols = [c.volume for c in candles.candles[-20:]]
            avg_vol = sum(recent_vols) / len(recent_vols)
            current_vol = candles.candles[-1].volume
            vol_spike = current_vol / avg_vol if avg_vol > 0 else 1.0

        # H1 info
        h1_dir = mtf_info.get("h1_direction", "UNKNOWN")
        h1_conf = mtf_info.get("h1_confidence", 0.0)
        h1_dir_map = {"DOWN": 0, "FLAT": 1, "UP": 2}
        h1_dir_int = h1_dir_map.get(h1_dir, 1)

        return TradeFeatures(
            signal_strength=signal.strength,
            signal_confidence=signal.confidence,
            h1_direction=h1_dir_int,
            h1_confidence=h1_conf,
            atr_pct=atr_pct,
            volume_spike=vol_spike,
        )

    async def run(
        self,
        candles: CandleSeries,
        signal_generator,
        meta_filter: Callable = None,
        initial_capital: float = None,
        capture_features: bool = False,
    ) -> Tuple[BacktestResult, List[dict]]:
        """Run backtest with optional meta-labeling.

        Args:
            candles: Historical candle data
            signal_generator: Function(sym, candles) -> Signal
            meta_filter: Optional function(signal_features) -> bool for meta filtering
            initial_capital: Starting capital
            capture_features: If True, capture features for each trade

        Returns:
            (BacktestResult, trade_features_list)
        """
        self.reset()
        self.capital = initial_capital or self.config.initial_capital
        self.trade_features = []

        self.start_time = datetime.utcnow()
        logger.info(
            f"Starting meta-label backtest: {len(candles.candles)} candles, "
            f"initial_capital={self.capital}, use_meta={meta_filter is not None}"
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

            # Get signal and MTF info
            signal, mtf_info = await signal_generator(candles.symbol, visible_candles)

            # Meta-label filtering
            if meta_filter is not None and signal.is_actionable and signal.direction != SignalDirection.NEUTRAL:
                features = self._extract_trade_features(signal, mtf_info, visible_candles)
                try:
                    result = meta_filter(features)
                    if isinstance(result, tuple):
                        accept, _ = result
                    else:
                        accept = result
                    if not accept:
                        signal.direction = SignalDirection.NEUTRAL
                        signal.strength = 0.0
                except Exception:
                    pass  # If meta_filter fails, allow the signal

            if signal.is_actionable and signal.direction != SignalDirection.NEUTRAL:
                in_cooldown = (i - self._last_trade_candle) < self.config.cooldown_candles
                halted_by_drawdown = self._drawdown_halted
                if not in_cooldown and not halted_by_drawdown:
                    self._process_signal(signal, candle, visible_candles)
                    if self.positions and self.positions[-1].strategy_id:
                        self._last_trade_candle = i

                    # Capture features if requested
                    if capture_features:
                        features = self._extract_trade_features(signal, mtf_info, visible_candles)
                        self.trade_features.append(features)

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

        result = BacktestResult(
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

        return result, self.trade_features


def create_trade_features_extractor(mtf_info: dict, candles: CandleSeries):
    """Create a features extractor function bound to specific trade data."""
    def extract(trade: dict) -> TradeFeatures:
        # Use stored trade info; for backtest we reconstruct from trade dict
        return TradeFeatures(
            signal_strength=trade.get("signal_strength", 0.5),
            signal_confidence=trade.get("confidence", 0.5),
            h1_direction=trade.get("h1_direction", 1),
            h1_confidence=trade.get("h1_confidence", 0.0),
            atr_pct=trade.get("atr_pct", 0.001),
            volume_spike=trade.get("volume_spike", 1.0),
        )
    return extract


def create_labeled_trades_from_trades(
    trades: list[dict],
    mtf_infos: list[dict],
) -> List[LabeledTrade]:
    """Create LabeledTrade list from backtest trades and MTF info.

    Args:
        trades: List of trade dicts from backtest
        mtf_infos: List of MTF info dicts for each trade

    Returns:
        List of LabeledTrade for meta-model training
    """
    labeled_trades = []

    for i, trade in enumerate(trades):
        if "pnl" not in trade:
            continue

        mtf = mtf_infos[i] if i < len(mtf_infos) else {}

        label = 1 if trade["pnl"] > 0 else 0

        features = TradeFeatures(
            signal_strength=trade.get("signal_strength", 0.5),
            signal_confidence=trade.get("confidence", 0.5),
            h1_direction={"DOWN": 0, "FLAT": 1, "UP": 2}.get(mtf.get("h1_direction", "FLAT"), 1),
            h1_confidence=mtf.get("h1_confidence", 0.0),
            atr_pct=trade.get("atr_pct", 0.001),
            volume_spike=trade.get("volume_spike", 1.0),
        )

        labeled_trades.append(LabeledTrade(features=features, label=label))

    return labeled_trades


def print_comparison(no_meta, with_meta) -> None:
    """Print comparison table."""
    header = f"{'Metric':<20} {'No Meta':>12} {'With Meta':>12} {'Diff':>10}"
    print("\n" + "=" * 60)
    print("META-LABELING COMPARISON")
    print("=" * 60)
    print(header)
    print("-" * 60)

    metrics = [
        ("Total Trades", no_meta.total_trades, with_meta.total_trades),
        ("Win Rate", no_meta.win_rate * 100, with_meta.win_rate * 100, "%"),
        ("Max Drawdown", no_meta.max_drawdown, with_meta.max_drawdown, "%"),
        ("Sharpe Ratio", no_meta.sharpe_ratio, with_meta.sharpe_ratio),
        ("Return %", no_meta.total_return_pct, with_meta.total_return_pct, "%"),
        ("Final Capital", no_meta.final_capital, with_meta.final_capital, "$"),
    ]

    for m in metrics:
        name = m[0]
        no_val = m[1]
        with_val = m[2]
        unit = m[3] if len(m) > 3 else None

        if unit == "%":
            diff = with_val - no_val
            diff_str = f"+{diff:.1f}%" if diff >= 0 else f"{diff:.1f}%"
            print(f"{name:<20} {no_val:>11.1f}% {with_val:>11.1f}% {diff_str:>10}")
        elif unit == "$":
            diff = with_val - no_val
            diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
            print(f"{name:<20} {no_val:>12.2f} {with_val:>12.2f} {diff_str:>10}")
        else:
            diff = with_val - no_val
            diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
            print(f"{name:<20} {no_val:>12.2f} {with_val:>12.2f} {diff_str:>10}")

    print("-" * 60)
    if with_meta.win_rate > no_meta.win_rate:
        print("✓ Meta-labeling improves win rate")
    if with_meta.sharpe_ratio > no_meta.sharpe_ratio:
        print("✓ Meta-labeling improves Sharpe ratio")
    if with_meta.total_trades < no_meta.total_trades:
        print(f"✓ Meta-labeling filters {no_meta.total_trades - with_meta.total_trades} trades")


def save_results_md(no_meta, with_meta, date_str: str) -> Path:
    """Save results to markdown file."""
    filepath = Path(f"results/meta_label_backtest_{date_str}.md")
    filepath.parent.mkdir(parents=True, exist_ok=True)

    trades_filtered = no_meta.total_trades - with_meta.total_trades

    lines = [
        "# Meta-Labeling Backtest Results",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat()}",
        "\n## Configuration",
        "- Symbol: BTCUSDT",
        "- Backtest Period: 7 days",
        "- Initial Capital: 10,000 USDT",
        "\n## Results",
        "\n| Metric | No Meta | With Meta | Difference |",
        "|---|---|---|---|",
        f"| Total Trades | {no_meta.total_trades} | {with_meta.total_trades} | {trades_filtered:+d} filtered |",
        f"| Win Rate | {no_meta.win_rate*100:.1f}% | {with_meta.win_rate*100:.1f}% | {(with_meta.win_rate - no_meta.win_rate)*100:+.1f}% |",
        f"| Max Drawdown | {no_meta.max_drawdown:.2f}% | {with_meta.max_drawdown:.2f}% | {with_meta.max_drawdown - no_meta.max_drawdown:+.2f}% |",
        f"| Sharpe Ratio | {no_meta.sharpe_ratio:.2f} | {with_meta.sharpe_ratio:.2f} | {with_meta.sharpe_ratio - no_meta.sharpe_ratio:+.2f} |",
        f"| Return | {no_meta.total_return_pct:.2f}% | {with_meta.total_return_pct:.2f}% | {with_meta.total_return_pct - no_meta.total_return_pct:+.2f}% |",
        f"| Final Capital | ${no_meta.final_capital:.2f} | ${with_meta.final_capital:.2f} | ${with_meta.final_capital - no_meta.final_capital:+.2f} |",
        "\n## Interpretation",
        f"- Meta-labeling filtered {trades_filtered} trades ({trades_filtered/no_meta.total_trades*100:.1f}% of signals)",
        f"- Win rate change: {no_meta.win_rate*100:.1f}% → {with_meta.win_rate*100:.1f}%",
        "- If win rate improves, meta-labeling is effectively filtering low-quality signals",
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
        print("WARNING: H1Model not found")

    # 3. Setup strategy with MTF info capture
    config_h1 = MomentumConfig(
        name="h1_test",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config_h1, h1_model=h1_model)

    # Track MTF info for each signal
    mtf_history: List[dict] = []

    async def signal_gen(sym, c):
        # generate_signal returns just Signal, but MetaLabelBacktestEngine expects tuple
        # We need to extract MTF info separately
        signal = await strategy.generate_signal(sym, c, None)
        # Get H1Model info directly from strategy if available
        mtf_info = {"h1_direction": "UNKNOWN", "h1_confidence": 0.0}
        return (signal, mtf_info)

    # 4. Run backtest WITHOUT meta-labeling (to capture trades for training)
    print("\nRunning backtest WITHOUT meta-labeling...")
    config = BacktestConfig(
        initial_capital=10000.0,
        use_trailing_stop=True,
        trailing_activation_atr=2.5,
        trailing_distance_atr=2.5,
        use_atr_stops=False,
        atr_sl_multiplier=99.0,
        atr_tp_multiplier=99.0,
    )

    engine = MetaLabelBacktestEngine(config, meta_model=None)
    result_no_meta, features_no_meta = await engine.run(
        candles_1m, signal_gen, meta_filter=None, capture_features=True
    )

    print(f"  Trades: {result_no_meta.total_trades}, Sharpe: {result_no_meta.sharpe_ratio:.2f}")

    # 5. Build meta-labeling training data from first backtest trades
    print("\nPreparing meta-labeling training data...")
    trades = engine.trades

    labeled_trades = []
    for i, trade in enumerate(trades):
        if "pnl" not in trade:
            continue

        mtf = mtf_history[i] if i < len(mtf_history) else {}

        # Skip if we don't have PnL
        pnl = trade.get("pnl", 0)
        if pnl is None:
            continue

        label = 1 if pnl > 0 else 0

        # Get H1 direction
        h1_dir_str = mtf.get("h1_direction", "FLAT")
        h1_dir_int = {"DOWN": 0, "FLAT": 1, "UP": 2}.get(h1_dir_str, 1)

        features = TradeFeatures(
            signal_strength=trade.get("signal_strength", 0.5),
            signal_confidence=trade.get("confidence", 0.5),
            h1_direction=h1_dir_int,
            h1_confidence=mtf.get("h1_confidence", 0.0),
            atr_pct=trade.get("atr_pct", 0.001),
            volume_spike=trade.get("volume_spike", 1.0),
        )

        labeled_trades.append(LabeledTrade(features=features, label=label))

    print(f"  Labeled trades: {len(labeled_trades)} (wins={sum(1 for t in labeled_trades if t.label==1)}, losses={sum(1 for t in labeled_trades if t.label==0)})")

    # 6. Train meta-labeling model
    meta_model = MetaLabelModel(MetaLabelConfig(epochs=10))
    if len(labeled_trades) >= 10:
        print("\nTraining meta-labeling model...")
        history = meta_model.train(labeled_trades, val_split=0.2)
        print(f"  Training complete: {len(labeled_trades)} trades")
    else:
        print("WARNING: Insufficient trades for meta-labeling training")

    # 7. Run backtest WITH meta-labeling
    print("\nRunning backtest WITH meta-labeling...")

    # Create meta filter from trained model
    def meta_filter(features: TradeFeatures) -> bool:
        accept, _ = meta_model.should_accept(features)
        return accept

    engine_meta = MetaLabelBacktestEngine(config, meta_model=meta_model)

    # Reset mtf history for second run
    mtf_history2 = []

    async def signal_gen_meta(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        mtf_history2.append(mtf)
        return (signal, mtf)

    result_with_meta, _ = await engine_meta.run(
        candles_1m, signal_gen_meta, meta_filter=meta_filter
    )

    print(f"  Trades: {result_with_meta.total_trades}, Sharpe: {result_with_meta.sharpe_ratio:.2f}")

    # 8. Print comparison
    print_comparison(result_no_meta, result_with_meta)

    # 9. Save results
    filepath = save_results_md(result_no_meta, result_with_meta, date_str)
    print(f"\nResults saved to: {filepath}")


if __name__ == "__main__":
    asyncio.run(main())