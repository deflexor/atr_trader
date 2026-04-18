"""Quick backtest without ML to validate strategy changes."""

import asyncio
import logging
from datetime import datetime

from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.strategies.mean_reversion_strategy import MeanReversionStrategy, MeanReversionConfig
from src.core.db.datastore import DataStore
from src.core.models.candle import CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def get_candles(datastore, symbol, timeframe, start, end):
    ts_start = int(start.timestamp())
    ts_end = int(end.timestamp())
    c = datastore.get_candles(symbol, "bybit", timeframe, ts_start, ts_end)
    return CandleSeries(candles=c, symbol=symbol, exchange="bybit", timeframe=timeframe)


async def backtest_strategy(strategy_name, strategy, test_candles, initial_capital=10000.0):
    """Run backtest for a strategy."""
    engine = BacktestEngine()

    async def signal_generator(sym, candles):
        return await strategy.generate_signal(sym, candles)

    result = await engine.run(test_candles, signal_generator, initial_capital)

    return {
        "strategy": strategy_name,
        "return_pct": result.total_return_pct,
        "sharpe": result.sharpe_ratio,
        "win_rate": result.win_rate,
        "trades": result.total_trades,
        "max_dd": result.max_drawdown,
    }


async def main():
    symbol = "BTCUSDT"
    timeframe = "5m"

    backtest_start = datetime.strptime("2025-11-16", "%Y-%m-%d")
    backtest_end = datetime.strptime("2026-01-15", "%Y-%m-%d")

    datastore = DataStore()
    test_candles = get_candles(datastore, symbol, timeframe, backtest_start, backtest_end)

    logger.info(f"Backtest period: {backtest_start.date()} to {backtest_end.date()}")
    logger.info(f"Candles: {len(test_candles.candles)}")

    # Test momentum
    logger.info("\n--- Testing Momentum Strategy ---")
    momentum = MomentumStrategy(MomentumConfig(name="momentum"))
    momentum_result = await backtest_strategy("momentum", momentum, test_candles)

    # Test mean reversion
    logger.info("\n--- Testing Mean Reversion Strategy ---")
    mean_rev = MeanReversionStrategy(MeanReversionConfig(name="mean_reversion"))
    mean_rev_result = await backtest_strategy("mean_reversion", mean_rev, test_candles)

    # Summary
    logger.info("\n" + "=" * 70)
    logger.info("BACKTEST RESULTS SUMMARY (Updated)")
    logger.info("=" * 70)
    logger.info(f"{'Strategy':<20} {'Return%':<12} {'Sharpe':<10} {'WinRate':<10} {'Trades':<8} {'MaxDD':<10}")
    logger.info("-" * 70)

    for result in [momentum_result, mean_rev_result]:
        logger.info(
            f"{result['strategy']:<20} {result['return_pct']:>10.2f}% {result['sharpe']:>9.2f} "
            f"{result['win_rate']:>9.1%} {result['trades']:>7} ${result['max_dd']:>8.2f}"
        )

    logger.info("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())