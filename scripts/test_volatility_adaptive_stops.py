"""Comparison test: volatility-adaptive vs fixed ATR trailing stops.

Runs back-to-back DOGE 120-day 5m backtest with and without volatility adaptation.
"""

import asyncio
import logging
from datetime import datetime

from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
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


async def run_backtest(test_candles, volatility_enabled: bool, initial_capital: float = 10000.0):
    """Run backtest with specified volatility adjustment setting."""
    config = MomentumConfig(name="momentum")
    config.volatility_adjustment_enabled = volatility_enabled
    strategy = MomentumStrategy(config)

    engine_config = BacktestConfig(
        initial_capital=initial_capital,
        use_trailing_stop=True,
        trailing_activation_atr=config.trailing_activation_atr,
        trailing_distance_atr=config.trailing_distance_atr,
        volatility_adjustment_enabled=volatility_enabled,
        volatility_lookback=config.volatility_lookback,
    )
    engine = BacktestEngine(config=engine_config)

    async def signal_generator(sym, candles):
        return await strategy.generate_signal(sym, candles)

    result = await engine.run(test_candles, signal_generator, initial_capital)

    return {
        "volatility_enabled": volatility_enabled,
        "return_pct": result.total_return_pct,
        "sharpe": result.sharpe_ratio,
        "win_rate": result.win_rate,
        "total_trades": result.total_trades,
        "max_drawdown": result.max_drawdown,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "avg_win": result.avg_win,
        "avg_loss": result.avg_loss,
        "final_capital": result.final_capital,
    }


async def main():
    symbol = "DOGEUSDT"
    timeframe = "5m"

    # 120-day backtest period
    backtest_start = datetime.strptime("2025-09-01", "%Y-%m-%d")
    backtest_end = datetime.strptime("2026-01-01", "%Y-%m-%d")

    datastore = DataStore()
    test_candles = get_candles(datastore, symbol, timeframe, backtest_start, backtest_end)

    logger.info(f"Symbol: {symbol}, Timeframe: {timeframe}")
    logger.info(f"Backtest period: {backtest_start.date()} to {backtest_end.date()}")
    logger.info(f"Candles: {len(test_candles.candles)}")
    logger.info("")

    # Run WITHOUT volatility adaptation (baseline)
    logger.info("=== Running WITHOUT volatility adaptation (baseline) ===")
    baseline_result = await run_backtest(test_candles, volatility_enabled=False)

    # Run WITH volatility adaptation
    logger.info("=== Running WITH volatility adaptation ===")
    adaptive_result = await run_backtest(test_candles, volatility_enabled=True)

    # Comparison report
    logger.info("")
    logger.info("=" * 80)
    logger.info("COMPARISON RESULTS: Volatility-Adaptive vs Fixed ATR Trailing Stops")
    logger.info("=" * 80)

    logger.info(f"{'Metric':<25} {'Fixed (Baseline)':<20} {'Volatility-Adaptive':<20} {'Difference':<15}")
    logger.info("-" * 80)
    logger.info(f"{'Return %':<25} {baseline_result['return_pct']:>18.2f}% {adaptive_result['return_pct']:>18.2f}% {adaptive_result['return_pct'] - baseline_result['return_pct']:>+14.2f}%")
    logger.info(f"{'Sharpe Ratio':<25} {baseline_result['sharpe']:>18.2f} {adaptive_result['sharpe']:>18.2f} {adaptive_result['sharpe'] - baseline_result['sharpe']:>+14.2f}")
    logger.info(f"{'Win Rate':<25} {baseline_result['win_rate']:>17.1%} {adaptive_result['win_rate']:>17.1%} {adaptive_result['win_rate'] - baseline_result['win_rate']:>+14.1%}")
    logger.info(f"{'Total Trades':<25} {baseline_result['total_trades']:>18} {adaptive_result['total_trades']:>18} {adaptive_result['total_trades'] - baseline_result['total_trades']:>+14}")
    logger.info(f"{'Winning Trades':<25} {baseline_result['winning_trades']:>18} {adaptive_result['winning_trades']:>18} {adaptive_result['winning_trades'] - baseline_result['winning_trades']:>+14}")
    logger.info(f"{'Losing Trades':<25} {baseline_result['losing_trades']:>18} {adaptive_result['losing_trades']:>18} {adaptive_result['losing_trades'] - baseline_result['losing_trades']:>+14}")
    logger.info(f"{'Max Drawdown':<25} {baseline_result['max_drawdown']:>17.1%} {adaptive_result['max_drawdown']:>17.1%} {adaptive_result['max_drawdown'] - baseline_result['max_drawdown']:>+14.1%}")
    logger.info(f"{'Avg Win':<25} ${baseline_result['avg_win']:>17.2f} ${adaptive_result['avg_win']:>17.2f} ${adaptive_result['avg_win'] - baseline_result['avg_win']:>+13.2f}")
    logger.info(f"{'Avg Loss':<25} ${baseline_result['avg_loss']:>17.2f} ${adaptive_result['avg_loss']:>17.2f} ${adaptive_result['avg_loss'] - baseline_result['avg_loss']:>+13.2f}")
    logger.info(f"{'Final Capital':<25} ${baseline_result['final_capital']:>17.2f} ${adaptive_result['final_capital']:>17.2f} ${adaptive_result['final_capital'] - baseline_result['final_capital']:>+13.2f}")
    logger.info("=" * 80)

    # Success criteria check
    logger.info("")
    logger.info("SUCCESS CRITERIA CHECK:")
    target_return = 8.64
    if adaptive_result['return_pct'] > target_return:
        logger.info(f"  [PASS] Volatility-adaptive return {adaptive_result['return_pct']:.2f}% > {target_return}% target")
    else:
        logger.info(f"  [FAIL] Volatility-adaptive return {adaptive_result['return_pct']:.2f}% <= {target_return}% target")
    
    if adaptive_result['return_pct'] > baseline_result['return_pct']:
        logger.info(f"  [PASS] Volatility-adaptive ({adaptive_result['return_pct']:.2f}%) outperforms baseline ({baseline_result['return_pct']:.2f}%)")
    else:
        logger.info(f"  [FAIL] Volatility-adaptive ({adaptive_result['return_pct']:.2f}%) does not outperform baseline ({baseline_result['return_pct']:.2f}%)")


if __name__ == "__main__":
    asyncio.run(main())
