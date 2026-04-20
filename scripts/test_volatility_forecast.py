"""Comparison test: baseline vs reactive volatility vs Holt-Winters forecast.

Tests three modes:
1. Fixed ATR trailing stops (baseline)
2. Reactive volatility adjustment (current vol)
3. Holt-Winters forecasted volatility (proactive)

Uses fecon235's Holt-Winters methodology for volatility regime forecasting.
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


async def run_backtest(
    test_candles,
    mode: str,
    initial_capital: float = 10000.0
):
    """Run backtest in specified mode.

    Modes:
        - "baseline": Fixed ATR multipliers (no adjustment)
        - "reactive": Adjust based on current realized volatility
        - "forecast": Adjust based on Holt-Winters forecasted volatility
    """
    config = MomentumConfig(name="momentum")

    if mode == "baseline":
        config.volatility_adjustment_enabled = False
    elif mode == "reactive":
        config.volatility_adjustment_enabled = True
        config.use_volatility_forecast = False
    elif mode == "forecast":
        config.volatility_adjustment_enabled = True
        config.use_volatility_forecast = True
    else:
        raise ValueError(f"Unknown mode: {mode}")

    strategy = MomentumStrategy(config)

    engine_config = BacktestConfig(
        initial_capital=initial_capital,
        use_trailing_stop=True,
        trailing_activation_atr=config.trailing_activation_atr,
        trailing_distance_atr=config.trailing_distance_atr,
        volatility_adjustment_enabled=config.volatility_adjustment_enabled,
        volatility_lookback=config.volatility_lookback,
    )
    engine = BacktestEngine(config=engine_config)

    async def signal_generator(sym, candles):
        return await strategy.generate_signal(sym, candles)

    result = await engine.run(test_candles, signal_generator, initial_capital)

    return {
        "mode": mode,
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

    # 120-day backtest period (Sep-Jan)
    backtest_start = datetime.strptime("2025-09-01", "%Y-%m-%d")
    backtest_end = datetime.strptime("2026-01-01", "%Y-%m-%d")

    datastore = DataStore()
    test_candles = get_candles(datastore, symbol, timeframe, backtest_start, backtest_end)

    logger.info(f"Symbol: {symbol}, Timeframe: {timeframe}")
    logger.info(f"Backtest period: {backtest_start.date()} to {backtest_end.date()}")
    logger.info(f"Candles: {len(test_candles.candles)}")
    logger.info("")

    # Run all three modes
    modes = ["baseline", "reactive", "forecast"]
    results = {}

    for mode in modes:
        logger.info(f"=== Running {mode.upper()} mode ===")
        results[mode] = await run_backtest(test_candles, mode)
        logger.info(f"  Return: {results[mode]['return_pct']:.2f}%")
        logger.info(f"  Trades: {results[mode]['total_trades']}")
        logger.info("")

    # Comparison report
    logger.info("=" * 100)
    logger.info("COMPARISON RESULTS: Baseline vs Reactive vs Holt-Winters Forecast")
    logger.info("=" * 100)

    headers = ["Metric", "Baseline (Fixed)", "Reactive (Current Vol)", "Forecast (Holt-Winters)"]
    logger.info(f"{headers[0]:<25} {headers[1]:<20} {headers[2]:<25} {headers[3]:<25}")
    logger.info("-" * 100)

    metrics = [
        ("Return %", "return_pct"),
        ("Sharpe Ratio", "sharpe"),
        ("Win Rate", "win_rate"),
        ("Total Trades", "total_trades"),
        ("Max Drawdown", "max_drawdown"),
        ("Avg Win", "avg_win"),
        ("Avg Loss", "avg_loss"),
        ("Final Capital", "final_capital"),
    ]

    for name, key in metrics:
        if key == "win_rate":
            baseline = f"{results['baseline'][key]:.1%}"
            reactive = f"{results['reactive'][key]:.1%}"
            forecast = f"{results['forecast'][key]:.1%}"
        elif key in ["return_pct", "max_drawdown"]:
            baseline = f"{results['baseline'][key]:.2f}%"
            reactive = f"{results['reactive'][key]:.2f}%"
            forecast = f"{results['forecast'][key]:.2f}%"
        else:
            baseline = f"{results['baseline'][key]:.2f}"
            reactive = f"{results['reactive'][key]:.2f}"
            forecast = f"{results['forecast'][key]:.2f}"

        improvement_reactive = results['reactive'][key] - results['baseline'][key]
        improvement_forecast = results['forecast'][key] - results['baseline'][key]

        if key == "win_rate":
            diff_reactive = f"{improvement_reactive:+.1%}"
            diff_forecast = f"{improvement_forecast:+.1%}"
        elif key in ["return_pct", "max_drawdown"]:
            diff_reactive = f"{improvement_reactive:+.2f}%"
            diff_forecast = f"{improvement_forecast:+.2f}%"
        else:
            diff_reactive = f"{improvement_reactive:+.2f}"
            diff_forecast = f"{improvement_forecast:+.2f}"

        logger.info(f"{name:<25} {baseline:<20} {reactive:<15} ({diff_reactive}) {forecast:<15} ({diff_forecast})")

    logger.info("=" * 100)

    # Success criteria
    logger.info("")
    logger.info("SUCCESS CRITERIA CHECK:")

    # Check if forecast outperforms reactive
    if results['forecast']['return_pct'] > results['reactive']['return_pct']:
        logger.info(f"  [PASS] Forecast ({results['forecast']['return_pct']:.2f}%) outperforms Reactive ({results['reactive']['return_pct']:.2f}%)")
    else:
        logger.info(f"  [FAIL] Forecast ({results['forecast']['return_pct']:.2f}%) <= Reactive ({results['reactive']['return_pct']:.2f}%)")

    # Check if forecast is better than baseline
    if results['forecast']['return_pct'] > results['baseline']['return_pct']:
        improvement = results['forecast']['return_pct'] - results['baseline']['return_pct']
        logger.info(f"  [PASS] Forecast ({results['forecast']['return_pct']:.2f}%) better than Baseline ({results['baseline']['return_pct']:.2f}%) by {improvement:.2f}%")
    else:
        logger.info(f"  [FAIL] Forecast ({results['forecast']['return_pct']:.2f}%) <= Baseline ({results['baseline']['return_pct']:.2f}%)")

    # Check if all modes are functional
    if all(results[m]['return_pct'] > 0 for m in modes):
        logger.info("  [PASS] All modes return positive returns")
    else:
        logger.info("  [FAIL] Some modes return negative")

    logger.info("")
    logger.info("INTERPRETATION:")
    logger.info("  - Baseline: Fixed ATR multipliers, no volatility adjustment")
    logger.info("  - Reactive: Adjusts based on CURRENT realized volatility (after it changes)")
    logger.info("  - Forecast: Adjusts based on Holt-Winters FORECASTED volatility (before it changes)")


if __name__ == "__main__":
    asyncio.run(main())