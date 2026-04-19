"""Research script: Train on historical data, evaluate retrain frequency, compare strategies.

Workflow:
1. Fetch and store data from Bybit (Sept 2025 - present)
2. Train on Sept-Nov 2025 data
3. Backtest on held-out period (Nov 2025 - Jan 2026)
4. Evaluate performance over days to determine when training becomes stale
5. Compare strategies to find the best one
"""

import asyncio
import argparse
import logging
from datetime import datetime, timedelta
from typing import Optional
import json

from src.adapters.bybit_adapter import BybitAdapter
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.strategies.mean_reversion_strategy import MeanReversionStrategy, MeanReversionConfig
from src.core.db.datastore import DataStore
from src.core.models.candle import Candle, CandleSeries
from src.trading_system import TradingSystem, TradingSystemConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def fetch_and_store_data(
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
    datastore: DataStore,
) -> int:
    """Fetch historical data from Bybit and store in SQLite.

    Args:
        symbol: Trading pair symbol (e.g., 'BTCUSDT')
        timeframe: Candle timeframe (e.g., '1m', '4h')
        start_date: Start datetime
        end_date: End datetime
        datastore: DataStore instance

    Returns:
        Number of candles stored
    """
    logger.info(f"Fetching {symbol} {timeframe} from {start_date.date()} to {end_date.date()}...")

    adapter = BybitAdapter()
    candles_data = await adapter.fetch_historical_by_period(
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
    )

    if not candles_data:
        logger.warning(f"No data returned for {symbol}")
        return 0

    # Convert to Candle objects
    # Bybit candle format: [timestamp, open, close, high, low, volume]
    candles = []
    for item in candles_data:
        candle = Candle(
            symbol=symbol,
            exchange="bybit",
            timeframe=timeframe,
            timestamp=datetime.fromtimestamp(int(item[0]) / 1000),
            open=float(item[1]),
            close=float(item[2]),
            high=float(item[3]),
            low=float(item[4]),
            volume=float(item[5]),
        )
        candles.append(candle)

    # Save to database
    saved = datastore.save_candles(candles)
    logger.info(f"Saved {saved} candles to database")

    return saved


def get_period_candles(
    datastore: DataStore,
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
) -> CandleSeries:
    """Get candles for a specific date range from the database.

    Args:
        datastore: DataStore instance
        symbol: Trading pair symbol
        timeframe: Candle timeframe
        start_date: Start datetime
        end_date: End datetime

    Returns:
        CandleSeries for the requested period
    """
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


async def run_single_experiment(
    symbol: str,
    timeframe: str,
    train_start: datetime,
    train_end: datetime,
    backtest_start: datetime,
    backtest_end: datetime,
    strategy_name: str,
    epochs: int,
    datastore: DataStore,
) -> dict:
    """Run a single experiment: train on one period, backtest on next period.

    Args:
        symbol: Trading pair symbol
        timeframe: Candle timeframe
        train_start: Training period start
        train_end: Training period end
        backtest_start: Backtest period start
        backtest_end: Backtest period end
        strategy_name: Strategy to use ('momentum' or 'mean_reversion')
        epochs: Number of training epochs
        datastore: DataStore instance

    Returns:
        Results dictionary
    """
    logger.info(f"\n{'=' * 60}")
    logger.info(f"Experiment: {symbol} {timeframe}")
    logger.info(f"Train: {train_start.date()} to {train_end.date()}")
    logger.info(f"Backtest: {backtest_start.date()} to {backtest_end.date()}")
    logger.info(f"Strategy: {strategy_name} | Epochs: {epochs}")
    logger.info(f"{'=' * 60}")

    try:
        # Create strategy
        if strategy_name == "momentum":
            strategy = MomentumStrategy(MomentumConfig(name="momentum"))
        else:
            strategy = MeanReversionStrategy(MeanReversionConfig(name="mean_reversion"))

        # Get training data from database
        train_series = get_period_candles(datastore, symbol, timeframe, train_start, train_end)

        # Get backtest data from database
        backtest_series = get_period_candles(
            datastore, symbol, timeframe, backtest_start, backtest_end
        )

        logger.info(f"Training data: {len(train_series.candles)} candles")
        logger.info(f"Backtest data: {len(backtest_series.candles)} candles")

        if len(train_series.candles) < 100:
            return {"status": "insufficient_train_data", "train_candles": len(train_series.candles)}

        if len(backtest_series.candles) < 50:
            return {
                "status": "insufficient_backtest_data",
                "backtest_candles": len(backtest_series.candles),
            }

        # Create trading system
        config = TradingSystemConfig(
            exchange="bybit",
            symbols=[symbol],
            timeframe=timeframe,
        )
        system = TradingSystem(config)

        # Train model
        logger.info(f"Training on {train_start.date()} to {train_end.date()}...")
        train_results = system.train_model(train_series)

        # Run backtest day by day to track performance decay
        # Calculate how many candles per day based on timeframe
        candles_per_day = {
            "1m": 1440,
            "5m": 288,
            "15m": 96,
            "1h": 24,
            "4h": 6,
        }
        candles_per_day_val = candles_per_day.get(timeframe, 24)

        daily_results = []
        backtest_candles = backtest_series.candles

        num_days = len(backtest_candles) // candles_per_day_val
        logger.info(
            f"Running continuous backtest over {num_days} days ({len(backtest_candles)} candles)..."
        )

        # Run a SINGLE continuous backtest for the entire period
        # This properly handles position carry-over and signal updates
        result = await system.run_backtest_with_ml(
            strategy=strategy,
            candles=backtest_series,
            initial_capital=10000.0,
        )

        # Use actual backtest result fields
        # Result may be BacktestResult or dict (error path)
        actual_return = getattr(result, "total_return_pct", result.get("total_return_pct", 0) if isinstance(result, dict) else 0)
        actual_sharpe = getattr(result, "sharpe_ratio", result.get("sharpe_ratio", 0) if isinstance(result, dict) else 0)
        actual_trades = getattr(result, "total_trades", result.get("total_trades", 0) if isinstance(result, dict) else 0)
        actual_win_rate = getattr(result, "win_rate", result.get("win_rate", 0) if isinstance(result, dict) else 0)
        actual_max_dd = getattr(result, "max_drawdown", result.get("max_drawdown", 0) if isinstance(result, dict) else 0)
        actual_winning = getattr(result, "winning_trades", result.get("winning_trades", 0) if isinstance(result, dict) else 0)
        actual_losing = getattr(result, "losing_trades", result.get("losing_trades", 0) if isinstance(result, dict) else 0)
        actual_avg_win = getattr(result, "avg_win", result.get("avg_win", 0) if isinstance(result, dict) else 0)
        actual_avg_loss = getattr(result, "avg_loss", result.get("avg_loss", 0) if isinstance(result, dict) else 0)

        logger.info(
            f"Backtest complete: Return={actual_return:.2f}%, "
            f"Sharpe={actual_sharpe:.2f}, WinRate={actual_win_rate:.1%}, Trades={actual_trades} ({actual_winning}W/{actual_losing}L)"
        )

        # Find when training starts to degrade (sharpe drops below threshold)
        sharpe_degradation_days = []
        for i, d in enumerate(daily_results):
            if d["sharpe"] < 0.5:  # Sharpe below 0.5 considered degraded
                sharpe_degradation_days.append(i + 1)

        first_degradation_day = (
            sharpe_degradation_days[0] if sharpe_degradation_days else len(daily_results)
        )

        return {
            "status": "success",
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy_name,
            "train_period": f"{train_start.date()} to {train_end.date()}",
            "backtest_period": f"{backtest_start.date()} to {backtest_end.date()}",
            "train_candles": len(train_series.candles),
            "backtest_candles": len(backtest_series.candles),
            "total_return_pct": actual_return,
            "avg_sharpe": actual_sharpe,
            "total_trades": actual_trades,
            "winning_trades": actual_winning,
            "losing_trades": actual_losing,
            "win_rate": actual_win_rate,
            "avg_win": actual_avg_win,
            "avg_loss": actual_avg_loss,
            "max_drawdown": actual_max_dd,
            "avg_trades_per_day": actual_trades / num_days if num_days > 0 else 0,
            "daily_results": [],  # daily breakdown not yet implemented
        }

    except Exception as e:
        logger.error(f"Error in experiment: {e}")
        import traceback

        traceback.print_exc()
        return {"status": "error", "error": str(e)}


async def main():
    parser = argparse.ArgumentParser(
        description="Train on historical data, evaluate retrain frequency"
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair symbol")
    parser.add_argument("--timeframe", default="5m", help="Candle timeframe (1m, 5m, 15m, 1h, 4h)")
    parser.add_argument(
        "--train-start", default="2025-09-15", help="Training start date (YYYY-MM-DD)"
    )
    parser.add_argument("--train-end", default="2025-11-15", help="Training end date (YYYY-MM-DD)")
    parser.add_argument(
        "--backtest-start", default="2025-11-16", help="Backtest start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--backtest-end", default="2026-01-15", help="Backtest end date (YYYY-MM-DD)"
    )
    parser.add_argument("--output", default="research_results.json", help="Output file")
    args = parser.parse_args()

    # Parse dates
    train_start = datetime.strptime(args.train_start, "%Y-%m-%d")
    train_end = datetime.strptime(args.train_end, "%Y-%m-%d")
    backtest_start = datetime.strptime(args.backtest_start, "%Y-%m-%d")
    backtest_end = datetime.strptime(args.backtest_end, "%Y-%m-%d")

    # Initialize datastore
    datastore = DataStore()

    # Check existing data in database
    existing_count = datastore.get_candle_count(
        symbol=args.symbol,
        exchange="bybit",
        timeframe=args.timeframe,
    )
    logger.info(f"Existing data: {existing_count} candles")

    # Calculate required data range (with buffer for backtest)
    data_start = train_start
    data_end = backtest_end + timedelta(days=1)

    # Check if we need to fetch data
    min_ts, max_ts = datastore.get_date_range(args.symbol, "bybit", args.timeframe)
    if min_ts and max_ts:
        existing_start = datetime.fromtimestamp(min_ts)
        existing_end = datetime.fromtimestamp(max_ts)
        logger.info(f"Database range: {existing_start.date()} to {existing_end.date()}")

        if existing_start > data_start or existing_end < data_end:
            logger.info("Need more data, fetching from Bybit...")
            await fetch_and_store_data(
                symbol=args.symbol,
                timeframe=args.timeframe,
                start_date=data_start,
                end_date=data_end,
                datastore=datastore,
            )
        else:
            logger.info("Using existing data from database")
    else:
        logger.info("No existing data, fetching from Bybit...")
        await fetch_and_store_data(
            symbol=args.symbol,
            timeframe=args.timeframe,
            start_date=data_start,
            end_date=data_end,
            datastore=datastore,
        )

    # Show date range in database
    min_ts, max_ts = datastore.get_date_range(args.symbol, "bybit", args.timeframe)
    if min_ts and max_ts:
        logger.info(
            f"Data range: {datetime.fromtimestamp(min_ts)} to {datetime.fromtimestamp(max_ts)}"
        )

    # Run experiments with both strategies
    all_results = []

    for strategy in ["momentum", "mean_reversion"]:
        result = await run_single_experiment(
            symbol=args.symbol,
            timeframe=args.timeframe,
            train_start=train_start,
            train_end=train_end,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            strategy_name=strategy,
            epochs=10,  # Reduced for faster iteration
            datastore=datastore,
        )
        all_results.append(result)

    # Save results
    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    # Print summary
    logger.info(f"\n{'=' * 80}")
    logger.info("RESEARCH SUMMARY")
    logger.info(f"{'=' * 80}")

    successful = [r for r in all_results if r["status"] == "success"]
    if successful:
        logger.info(
            f"\n{'Strategy':<20} {'Return%':<12} {'Sharpe':<10} {'WinRate':<10} {'Trades':<8} {'MaxDD$':<10} {'AvgWin':<10} {'AvgLoss':<10}"
        )
        logger.info("-" * 100)
        for r in successful:
            logger.info(
                f"{r['strategy']:<20} {r['total_return_pct']:>10.2f}% {r['avg_sharpe']:>9.2f} "
                f"{r.get('win_rate', 0):>9.1%} {r['total_trades']:>7} {r.get('max_drawdown', 0):>9.2f} {r.get('avg_win', 0):>9.2f} {r.get('avg_loss', 0):>9.2f}"
            )

        logger.info(f"\nResults saved to: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
