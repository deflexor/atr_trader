"""Diagnostic backtest to understand filter block rates.

Simplified version - no ML training, just strategy signal diagnostics.
"""

import asyncio
import logging
from datetime import datetime

from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.core.db.datastore import DataStore
from src.core.models.candle import CandleSeries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def get_period_candles(
    datastore: DataStore,
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
) -> CandleSeries:
    """Get candles for a specific date range from the database."""
    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())

    candles = datastore.get_candles(
        symbol=symbol,
        exchange="bybit",
        timeframe=timeframe,
        start_timestamp=start_ts,
        end_timestamp=end_ts,
    )

    return CandleSeries(
        candles=candles,
        symbol=symbol,
        exchange="bybit",
        timeframe=timeframe,
    )


async def run_diagnostic():
    """Run diagnostic backtest to understand filter block rates."""
    symbol = "BTCUSDT"
    timeframe = "5m"

    # Backtest period: Nov 2025 - Jan 2026
    backtest_start = datetime.strptime("2025-11-16", "%Y-%m-%d")
    backtest_end = datetime.strptime("2026-01-15", "%Y-%m-%d")

    datastore = DataStore()

    # Get backtest data
    backtest_series = get_period_candles(
        datastore, symbol, timeframe, backtest_start, backtest_end
    )

    logger.info(f"Backtest data: {len(backtest_series.candles)} candles")
    logger.info(f"Period: {backtest_start.date()} to {backtest_end.date()}")

    # Create strategy with diagnostics enabled (no ML - pure strategy signal)
    config = MomentumConfig(name="momentum")
    strategy = MomentumStrategy(config)

    # Reset diagnostics
    strategy.diagnostics = {
        "total_evaluated": 0,
        "atr_filtered": 0,
        "indicators_computed": 0,
        "min_agreement_passed": 0,
        "volume_filtered": 0,
        "mtf_filtered": 0,
        "pullback_filtered": 0,
        "rsi_divergence_filtered": 0,
        "entry_candle_filtered": 0,
        "signals_produced": 0,
    }

    # Process each candle
    total_candles = len(backtest_series.candles)
    non_neutral_signals = 0

    for i in range(50, total_candles):
        visible_candles = CandleSeries(
            candles=backtest_series.candles[: i + 1],
            symbol=symbol,
            exchange="bybit",
            timeframe=timeframe,
        )

        # Generate signal
        signal = await strategy.generate_signal(symbol, visible_candles)

        if signal.direction.name != "NEUTRAL":
            non_neutral_signals += 1

        # Progress logging
        if (i + 1) % 5000 == 0:
            logger.info(f"Progress: {i + 1}/{total_candles} candles processed...")

    # Print diagnostic report
    logger.info("\n" + "=" * 80)
    logger.info("DIAGNOSTIC REPORT: Momentum Strategy Filter Analysis")
    logger.info("=" * 80)

    d = strategy.diagnostics

    logger.info(f"\nTotal candles evaluated: {d['total_evaluated']}")
    logger.info(f"Non-NEUTRAL signals produced: {d['signals_produced']}")

    logger.info(f"\n--- FILTER BREAKDOWN ---")
    logger.info(f"ATR filter blocked:      {d['atr_filtered']:>6} ({d['atr_filtered']/max(d['total_evaluated'],1)*100:.1f}%)")
    logger.info(f"Min agreement passed:     {d['min_agreement_passed']:>6} ({d['min_agreement_passed']/max(d['total_evaluated'],1)*100:.1f}%)")
    logger.info(f"Volume spike blocked:     {d.get('volume_filtered', 0):>6}")
    logger.info(f"MTF filtered:             {d['mtf_filtered']:>6}")
    logger.info(f"Pullback filtered:        {d['pullback_filtered']:>6}")
    logger.info(f"RSI divergence filtered:  {d['rsi_divergence_filtered']:>6}")
    logger.info(f"Entry candle filtered:    {d['entry_candle_filtered']:>6}")

    # Calculate effective signal production rate
    if d['total_evaluated'] > 0:
        signal_rate = d['signals_produced'] / d['total_evaluated'] * 100
    else:
        signal_rate = 0

    logger.info(f"\n--- SIGNAL PRODUCTION ---")
    logger.info(f"Effective signal rate: {signal_rate:.3f}% of evaluated candles")
    logger.info(f"Expected trades over {d['total_evaluated']} candles: ~{d['signals_produced']} trades (with cooldown)")

    logger.info("\n" + "=" * 80)

    return d


if __name__ == "__main__":
    asyncio.run(run_diagnostic())