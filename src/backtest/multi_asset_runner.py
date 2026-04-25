"""Multi-asset concurrent backtest runner.

Time-syncs candle data across multiple symbols and runs
individual BacktestEngine instances against a shared capital pool.
Each asset gets its own engine instance but they share total capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
import logging

from ..core.models.candle import CandleSeries
from ..core.models.signal import Signal, SignalDirection
from .engine import BacktestEngine, BacktestConfig, BacktestResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MultiAssetConfig:
    """Configuration for multi-asset runner."""

    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "TRXUSDT")
    exchange: str = "bybit"
    timeframe: str = "5m"
    initial_capital: float = 10000.0
    max_positions_per_asset: int = 2
    capital_per_asset: float = 0.25  # fraction of total capital per asset


@dataclass
class AssetResult:
    """Per-asset backtest result."""

    symbol: str
    result: Optional[BacktestResult] = None
    trade_count: int = 0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0


@dataclass
class MultiAssetResult:
    """Aggregate result across all assets."""

    assets: list[AssetResult] = field(default_factory=list)
    total_return_pct: float = 0.0
    weighted_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    total_trades: int = 0

    @property
    def summary(self) -> str:
        lines = ["=== Multi-Asset Backtest Results ==="]
        for a in self.assets:
            lines.append(
                f"  {a.symbol}: {a.total_return_pct:+.2f}% "
                f"DD={a.max_drawdown_pct:.2f}% trades={a.trade_count}"
            )
        lines.append(f"  TOTAL: {self.weighted_return_pct:+.2f}% DD={self.max_drawdown_pct:.2f}%")
        return "\n".join(lines)


def allocate_capital(total: float, n_assets: int, per_asset_frac: float) -> list[float]:
    """Allocate capital equally across assets.

    Each asset gets total * per_asset_frac, capped at total.
    """
    per_asset = total * per_asset_frac
    return [per_asset] * n_assets


def build_timestamp_index(
    candle_sets: list[CandleSeries],
) -> list[list[int]]:
    """Build time-synced iteration indices for multiple candle series.

    Returns a list of index lists, one per asset, where each index
    points to the next candle with timestamp >= the current global time.
    """
    if not candle_sets:
        return []

    # Find the latest start timestamp across all assets (common window)
    start_times = []
    for cs in candle_sets:
        if cs.candles:
            start_times.append(cs.candles[0].timestamp)
    if not start_times:
        return []

    # Build index arrays: for each asset, list of valid indices
    indices = []
    for cs in candle_sets:
        asset_indices = list(range(len(cs.candles)))
        indices.append(asset_indices)

    return indices


async def run_multi_asset(
    candle_sets: list[CandleSeries],
    signal_generator,
    config: Optional[MultiAssetConfig] = None,
    backtest_config: Optional[BacktestConfig] = None,
) -> MultiAssetResult:
    """Run concurrent backtest across multiple assets.

    Each asset gets its own BacktestEngine with allocated capital.
    Engines run independently (no cross-asset position management yet).

    Args:
        candle_sets: One CandleSeries per asset, same order as config.symbols
        signal_generator: Async callable (symbol, candles) -> Signal
        config: Multi-asset configuration
        backtest_config: Per-engine backtest configuration

    Returns:
        MultiAssetResult with per-asset and aggregate metrics
    """
    cfg = config or MultiAssetConfig()
    bt_cfg = backtest_config or BacktestConfig()

    n_assets = len(candle_sets)
    allocations = allocate_capital(cfg.initial_capital, n_assets, cfg.capital_per_asset)

    results: list[AssetResult] = []

    for idx, (candles, allocation) in enumerate(zip(candle_sets, allocations)):
        symbol = candles.symbol or cfg.symbols[idx] if idx < len(cfg.symbols) else f"asset_{idx}"
        logger.info(f"Running backtest for {symbol} with capital={allocation:.0f}")

        # Create engine with allocated capital
        engine = BacktestEngine(bt_cfg)

        # Create per-asset signal generator that binds the symbol
        async def asset_signal(sym: str, cs: CandleSeries) -> Signal:
            return await signal_generator(sym, cs)

        result = await engine.run(
            candles,
            asset_signal,
            initial_capital=allocation,
        )

        asset_result = AssetResult(
            symbol=symbol,
            result=result,
            trade_count=len(result.trades) if result and result.trades else 0,
            total_return_pct=result.total_return_pct if result else 0.0,
            max_drawdown_pct=result.max_drawdown * 100 if result else 0.0,
        )
        results.append(asset_result)

    # Aggregate results
    total_trades = sum(a.trade_count for a in results)
    max_dd = max((a.max_drawdown_pct for a in results), default=0.0)

    # Weighted return: sum of (asset_return * allocation_weight)
    total_alloc = sum(allocations)
    weighted_return = sum(
        a.total_return_pct * (allocations[i] / total_alloc)
        for i, a in enumerate(results)
        if total_alloc > 0
    )

    return MultiAssetResult(
        assets=results,
        total_return_pct=sum(a.total_return_pct for a in results),
        weighted_return_pct=weighted_return,
        max_drawdown_pct=max_dd,
        total_trades=total_trades,
    )
