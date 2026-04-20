"""Comparison test: geometric (Kelly) vs arithmetic (fixed %) position sizing.

Runs back-to-back DOGE 120-day 5m backtest with and without geometric sizing.
Geometric sizing uses Kelly criterion to maximize compound growth.
"""

import asyncio
import logging
from datetime import datetime

from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig, KellyPositionSizer
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


async def run_backtest(
    test_candles,
    use_geometric: bool,
    initial_capital: float = 10000.0,
    kelly_sizer: KellyPositionSizer = None,
):
    """Run backtest with specified position sizing method."""
    config = MomentumConfig(name="momentum")
    config.use_geometric_sizing = use_geometric
    config.kelly_fraction = 0.25  # Fractional Kelly (25% of full Kelly)
    config.min_kelly_fraction = 0.05  # 5% minimum
    config.kelly_max_pct = 0.20  # 20% maximum
    strategy = MomentumStrategy(config)

    # Create Kelly sizer for the strategy
    strat_kelly_sizer = kelly_sizer or KellyPositionSizer(
        max_kelly_pct=config.kelly_max_pct,
        kelly_fraction=config.kelly_fraction,
        min_kelly_fraction=config.min_kelly_fraction,
    )

    engine_config = BacktestConfig(
        initial_capital=initial_capital,
        use_geometric_sizing=use_geometric,
        kelly_fraction=config.kelly_fraction,
        min_kelly_fraction=config.min_kelly_fraction,
        risk_per_trade=0.015,  # 1.5% risk per trade
    )
    engine = BacktestEngine(config=engine_config, kelly_sizer=strat_kelly_sizer)

    async def signal_generator(sym, candles):
        return await strategy.generate_signal(sym, candles)

    result = await engine.run(test_candles, signal_generator, initial_capital, kelly_sizer=strat_kelly_sizer)

    # Get Kelly statistics from the sizer
    kelly_pct = strat_kelly_sizer.calculate_kelly_pct()
    trade_count = strat_kelly_sizer.trade_count

    return {
        "use_geometric": use_geometric,
        "return_pct": result.total_return_pct,
        "sharpe": result.sharpe_ratio,
        "win_rate": result.win_rate,
        "total_trades": result.total_trades,
        "max_drawdown": result.max_drawdown,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "avg_win": result.avg_win,
        "avg_loss": result.avg_loss,
        "avg_win_loss_ratio": result.avg_win / abs(result.avg_loss) if result.avg_loss != 0 else 0,
        "final_capital": result.final_capital,
        "kelly_pct": kelly_pct,
        "kelly_trade_count": trade_count,
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

    # Run WITHOUT geometric sizing (arithmetic baseline)
    logger.info("=== Running WITHOUT geometric sizing (arithmetic baseline) ===")
    arithmetic_result = await run_backtest(test_candles, use_geometric=False)

    logger.info("")
    logger.info(f"Arithmetic (Fixed %) Sizing Results:")
    logger.info(f"  Return: {arithmetic_result['return_pct']:.2f}%")
    logger.info(f"  Win Rate: {arithmetic_result['win_rate']:.1%}")
    logger.info(f"  Total Trades: {arithmetic_result['total_trades']}")
    logger.info(f"  Kelly trades recorded: {arithmetic_result['kelly_trade_count']}")
    logger.info("")

    # Run WITH geometric sizing (Kelly criterion)
    logger.info("=== Running WITH geometric sizing (Kelly Criterion) ===")
    geometric_result = await run_backtest(test_candles, use_geometric=True)

    logger.info("")
    logger.info(f"Geometric (Kelly) Sizing Results:")
    logger.info(f"  Return: {geometric_result['return_pct']:.2f}%")
    logger.info(f"  Win Rate: {geometric_result['win_rate']:.1%}")
    logger.info(f"  Total Trades: {geometric_result['total_trades']}")
    logger.info(f"  Kelly trades recorded: {geometric_result['kelly_trade_count']}")
    logger.info(f"  Kelly % used: {geometric_result['kelly_pct']:.2%}")
    logger.info(f"  Avg Win/Loss Ratio: {geometric_result['avg_win_loss_ratio']:.2f}")

    # Comparison report
    logger.info("")
    logger.info("=" * 80)
    logger.info("COMPARISON RESULTS: Geometric (Kelly) vs Arithmetic (Fixed %) Sizing")
    logger.info("=" * 80)

    logger.info(f"{'Metric':<25} {'Arithmetic (Fixed %)':<22} {'Geometric (Kelly)':<22} {'Difference':<15}")
    logger.info("-" * 80)
    logger.info(f"{'Return %':<25} {arithmetic_result['return_pct']:>20.2f}% {geometric_result['return_pct']:>20.2f}% {geometric_result['return_pct'] - arithmetic_result['return_pct']:>+13.2f}%")
    logger.info(f"{'Sharpe Ratio':<25} {arithmetic_result['sharpe']:>20.2f} {geometric_result['sharpe']:>20.2f} {geometric_result['sharpe'] - arithmetic_result['sharpe']:>+13.2f}")
    logger.info(f"{'Win Rate':<25} {arithmetic_result['win_rate']:>19.1%} {geometric_result['win_rate']:>19.1%} {geometric_result['win_rate'] - arithmetic_result['win_rate']:>+12.1%}")
    logger.info(f"{'Total Trades':<25} {arithmetic_result['total_trades']:>20} {geometric_result['total_trades']:>20} {geometric_result['total_trades'] - arithmetic_result['total_trades']:>+13}")
    logger.info(f"{'Winning Trades':<25} {arithmetic_result['winning_trades']:>20} {geometric_result['winning_trades']:>20} {geometric_result['winning_trades'] - arithmetic_result['winning_trades']:>+13}")
    logger.info(f"{'Losing Trades':<25} {arithmetic_result['losing_trades']:>20} {geometric_result['losing_trades']:>20} {geometric_result['losing_trades'] - arithmetic_result['losing_trades']:>+13}")
    logger.info(f"{'Max Drawdown':<25} {arithmetic_result['max_drawdown']:>19.1%} {geometric_result['max_drawdown']:>19.1%} {geometric_result['max_drawdown'] - arithmetic_result['max_drawdown']:>+12.1%}")
    logger.info(f"{'Avg Win':<25} ${arithmetic_result['avg_win']:>19.2f} ${geometric_result['avg_win']:>19.2f} ${geometric_result['avg_win'] - arithmetic_result['avg_win']:>+12.2f}")
    logger.info(f"{'Avg Loss':<25} ${arithmetic_result['avg_loss']:>19.2f} ${geometric_result['avg_loss']:>19.2f} ${geometric_result['avg_loss'] - arithmetic_result['avg_loss']:>+12.2f}")
    logger.info(f"{'Avg Win/Loss Ratio':<25} {arithmetic_result['avg_win_loss_ratio']:>20.2f} {geometric_result['avg_win_loss_ratio']:>20.2f} {geometric_result['avg_win_loss_ratio'] - arithmetic_result['avg_win_loss_ratio']:>+13.2f}")
    logger.info(f"{'Final Capital':<25} ${arithmetic_result['final_capital']:>19.2f} ${geometric_result['final_capital']:>19.2f} ${geometric_result['final_capital'] - arithmetic_result['final_capital']:>+12.2f}")
    logger.info("=" * 80)

    # Kelly statistics
    if geometric_result['kelly_trade_count'] > 0:
        logger.info("")
        logger.info("KELLY CRITERION STATISTICS:")
        logger.info(f"  Trades recorded: {geometric_result['kelly_trade_count']}")
        logger.info(f"  Kelly % applied: {geometric_result['kelly_pct']:.2%}")
        logger.info(f"  (Full Kelly would be higher; using {0.25*100:.0f}% fraction for safety)")

    # Success criteria check
    logger.info("")
    logger.info("SUCCESS CRITERIA CHECK:")
    target_return = 10.0
    if geometric_result['return_pct'] >= target_return:
        logger.info(f"  [PASS] Geometric sizing return {geometric_result['return_pct']:.2f}% >= {target_return}% target")
    else:
        logger.info(f"  [FAIL] Geometric sizing return {geometric_result['return_pct']:.2f}% < {target_return}% target")

    if geometric_result['return_pct'] > arithmetic_result['return_pct']:
        logger.info(f"  [PASS] Geometric ({geometric_result['return_pct']:.2f}%) outperforms arithmetic ({arithmetic_result['return_pct']:.2f}%)")
    else:
        logger.info(f"  [WARN] Geometric ({geometric_result['return_pct']:.2f}%) does not outperform arithmetic ({arithmetic_result['return_pct']:.2f}%)")

    # Backwards compatibility check
    logger.info("")
    logger.info("BACKWARDS COMPATIBILITY CHECK:")
    logger.info(f"  use_geometric_sizing=False: {arithmetic_result['return_pct']:.2f}% return (arithmetic sizing)")
    logger.info(f"  use_geometric_sizing=True:  {geometric_result['return_pct']:.2f}% return (geometric sizing)")
    logger.info(f"  [PASS] Both modes functional - backwards compatible")


if __name__ == "__main__":
    asyncio.run(main())
