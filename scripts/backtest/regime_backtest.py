"""Market Regime Detection Backtest Script

Compares regime-aware strategy vs fixed momentum strategy:
- Regime-aware: switches between momentum (trending) and mean reversion (ranging)
- Fixed: always uses momentum strategy

Usage: uv run python scripts/backtest/regime_backtest.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.core.models.signal import Signal, SignalDirection
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.strategies.mean_reversion_strategy import MeanReversionStrategy, MeanReversionConfig
from src.strategies.regime_aware_strategy import (
    RegimeAwareStrategy,
    RegimeAwareConfig,
    MarketRegime,
    calculate_adx_pure,
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
    trending_trades: int = 0
    ranging_trades: int = 0


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


class RegimeBacktestEngine(BacktestEngine):
    """Backtest engine with regime tracking."""

    def __init__(self, config: BacktestConfig):
        super().__init__(config)
        self.trending_trades = 0
        self.ranging_trades = 0

    def reset(self) -> None:
        """Reset backtest state and regime counters."""
        super().reset()
        self.trending_trades = 0
        self.ranging_trades = 0


async def run_momentum_backtest(
    candles: CandleSeries,
    h1_model: H1Model,
) -> Tuple[BacktestMetrics, List[dict]]:
    """Run backtest with fixed momentum strategy."""
    config_h1 = MomentumConfig(
        name="momentum",
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

    engine = RegimeBacktestEngine(BacktestConfig(
        initial_capital=10000.0,
        use_trailing_stop=True,
        trailing_activation_atr=2.5,
        trailing_distance_atr=2.5,
        use_atr_stops=False,
        atr_sl_multiplier=99.0,
        atr_tp_multiplier=99.0,
    ))

    result = await engine.run(candles, signal_gen)

    return BacktestMetrics(
        total_trades=result.total_trades,
        win_rate=result.win_rate,
        max_drawdown=result.max_drawdown,
        sharpe_ratio=result.sharpe_ratio,
        total_return_pct=result.total_return_pct,
        avg_win=result.avg_win,
        avg_loss=result.avg_loss,
        final_capital=result.final_capital,
        trending_trades=engine.trending_trades,
        ranging_trades=engine.ranging_trades,
    ), engine.trades


async def run_regime_aware_backtest(
    candles: CandleSeries,
    h1_model: H1Model,
) -> Tuple[BacktestMetrics, List[dict]]:
    """Run backtest with regime-aware strategy (momentum + mean reversion)."""
    momentum_cfg = MomentumConfig(
        name="momentum",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    mean_rev_cfg = MeanReversionConfig(name="mean_reversion")

    regime_strategy = RegimeAwareStrategy(
        momentum_strategy=MomentumStrategy(config=momentum_cfg, h1_model=h1_model),
        mean_reversion_strategy=MeanReversionStrategy(config=mean_rev_cfg),
    )

    # Track regime per candle for diagnostics
    regime_counts = {"trending": 0, "ranging": 0}
    trades_with_regime: List[dict] = []

    async def signal_gen(sym, c):
        # Track regime
        regime = regime_strategy.detect_regime(c)
        regime_id = regime.value.lower()
        if regime_id == "trending":
            regime_counts["trending"] += 1
        else:
            regime_counts["ranging"] += 1

        signal = await regime_strategy.generate_signal(sym, c, None)

        # Annotate signal with regime for tracking
        if signal.direction != SignalDirection.NEUTRAL:
            signal.regime = regime_id

        return signal

    engine = RegimeBacktestEngine(BacktestConfig(
        initial_capital=10000.0,
        use_trailing_stop=True,
        trailing_activation_atr=2.5,
        trailing_distance_atr=2.5,
        use_atr_stops=False,
        atr_sl_multiplier=99.0,
        atr_tp_multiplier=99.0,
    ))

    result = await engine.run(candles, signal_gen)

    return BacktestMetrics(
        total_trades=result.total_trades,
        win_rate=result.win_rate,
        max_drawdown=result.max_drawdown,
        sharpe_ratio=result.sharpe_ratio,
        total_return_pct=result.total_return_pct,
        avg_win=result.avg_win,
        avg_loss=result.avg_loss,
        final_capital=result.final_capital,
        trending_trades=engine.trending_trades,
        ranging_trades=engine.ranging_trades,
    ), engine.trades


def print_comparison(momentum: BacktestMetrics, regime_aware: BacktestMetrics) -> None:
    """Print side-by-side comparison table."""
    header = f"{'Metric':<20} {'Momentum':>12} {'Regime-Aware':>15} {'Diff':>10}"
    print("\n" + "=" * 65)
    print("REGIME-AWARE vs FIXED MOMENTUM COMPARISON")
    print("=" * 65)
    print(header)
    print("-" * 65)

    metrics = [
        ("Total Trades", momentum.total_trades, regime_aware.total_trades),
        ("Win Rate", momentum.win_rate * 100, regime_aware.win_rate * 100, "%"),
        ("Max Drawdown", momentum.max_drawdown, regime_aware.max_drawdown, "%"),
        ("Sharpe Ratio", momentum.sharpe_ratio, regime_aware.sharpe_ratio),
        ("Return %", momentum.total_return_pct, regime_aware.total_return_pct, "%"),
        ("Avg Win", momentum.avg_win, regime_aware.avg_win, "$"),
        ("Avg Loss", momentum.avg_loss, regime_aware.avg_loss, "$"),
        ("Final Capital", momentum.final_capital, regime_aware.final_capital, "$"),
    ]

    for m in metrics:
        name = m[0]
        mom_val = m[1]
        reg_val = m[2]
        unit = m[3] if len(m) > 3 else None

        if unit == "%":
            diff = reg_val - mom_val
            diff_str = f"+{diff:.1f}%" if diff >= 0 else f"{diff:.1f}%"
            print(f"{name:<20} {mom_val:>11.1f}% {reg_val:>14.1f}% {diff_str:>10}")
        elif unit == "$":
            diff = reg_val - mom_val
            diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
            print(f"{name:<20} {mom_val:>12.2f} {reg_val:>15.2f} {diff_str:>10}")
        else:
            diff = reg_val - mom_val
            diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
            print(f"{name:<20} {mom_val:>12.2f} {reg_val:>15.2f} {diff_str:>10}")

    print("-" * 65)
    # Summary
    if regime_aware.sharpe_ratio > momentum.sharpe_ratio:
        print("✓ Regime-aware outperforms by Sharpe ratio")
    else:
        print("✗ Fixed momentum outperforms by Sharpe ratio")

    if regime_aware.total_trades != momentum.total_trades:
        print(f"  Trades difference: {regime_aware.total_trades - momentum.total_trades:+d}")


def save_results_md(momentum: BacktestMetrics, regime_aware: BacktestMetrics, date_str: str) -> Path:
    """Save results to markdown file."""
    filepath = Path(f"results/regime_backtest_{date_str}.md")
    filepath.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Market Regime Detection Backtest Results",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat()}",
        "\n## Configuration",
        "- Symbol: BTCUSDT",
        "- Backtest Period: 7 days",
        "- Initial Capital: 10,000 USDT",
        "- ADX Threshold: 25 (ADX > 25 = TRENDING, <= 25 = RANGING)",
        "\n## Strategy Selection",
        "- TRENDING (ADX > 25): Momentum strategy (trend following)",
        "- RANGING (ADX <= 25): Mean Reversion strategy (fade the move)",
        "\n## Results Comparison",
        "\n| Metric | Momentum | Regime-Aware | Difference |",
        "|---|---|---|---|",
        f"| Total Trades | {momentum.total_trades} | {regime_aware.total_trades} | {regime_aware.total_trades - momentum.total_trades:+d} |",
        f"| Win Rate | {momentum.win_rate*100:.1f}% | {regime_aware.win_rate*100:.1f}% | {(regime_aware.win_rate - momentum.win_rate)*100:+.1f}% |",
        f"| Max Drawdown | {momentum.max_drawdown:.2f}% | {regime_aware.max_drawdown:.2f}% | {regime_aware.max_drawdown - momentum.max_drawdown:+.2f}% |",
        f"| Sharpe Ratio | {momentum.sharpe_ratio:.2f} | {regime_aware.sharpe_ratio:.2f} | {regime_aware.sharpe_ratio - momentum.sharpe_ratio:+.2f} |",
        f"| Return | {momentum.total_return_pct:.2f}% | {regime_aware.total_return_pct:.2f}% | {regime_aware.total_return_pct - momentum.total_return_pct:+.2f}% |",
        f"| Final Capital | ${momentum.final_capital:.2f} | ${regime_aware.final_capital:.2f} | ${regime_aware.final_capital - momentum.final_capital:+.2f} |",
        "\n## Interpretation",
        "- Regime-aware adapts to market conditions by selecting appropriate strategy",
        "- If regime-aware outperforms, it suggests market regime switches are exploitable",
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

    # 3. Run momentum-only backtest
    print("\nRunning backtest with FIXED MOMENTUM strategy...")
    momentum_metrics, momentum_trades = await run_momentum_backtest(candles_1m, h1_model)
    print(f"  Trades: {momentum_metrics.total_trades}, Sharpe: {momentum_metrics.sharpe_ratio:.2f}")

    # 4. Run regime-aware backtest
    print("\nRunning backtest with REGIME-AWARE strategy...")
    regime_metrics, regime_trades = await run_regime_aware_backtest(candles_1m, h1_model)
    print(f"  Trades: {regime_metrics.total_trades}, Sharpe: {regime_metrics.sharpe_ratio:.2f}")

    # 5. Print comparison
    print_comparison(momentum_metrics, regime_metrics)

    # 6. Save results
    filepath = save_results_md(momentum_metrics, regime_metrics, date_str)
    print(f"\nResults saved to: {filepath}")


if __name__ == "__main__":
    asyncio.run(main())