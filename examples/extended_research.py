"""Extended research script for hyperparameter optimization.

Experiments:
1. Find optimal training period (1-month vs 2-month vs 3-month)
2. Find optimal retrain frequency (monthly vs bimonthly)
3. Compare performance across multiple assets

Usage:
    # Basic - BTC only, 3 month train, 2 month test
    python examples/extended_research.py

    # Full multi-asset study
    python examples/extended_research.py --assets BTC ETH DOGE TRX

    # Quick test (1 month each)
    python examples/extended_research.py --train-months 1 --test-months 1

    # Custom period
    python examples/extended_research.py --train-start 2025-10-01 --train-end 2025-12-31 --test-start 2026-01-01 --test-end 2026-02-28
"""

import asyncio
import argparse
import logging
import json
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from itertools import product

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


@dataclass
class ExperimentConfig:
    """Configuration for a single experiment."""

    symbol: str
    timeframe: str
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    strategy: str
    epochs: int = 20


@dataclass
class ExperimentResult:
    """Results from a single experiment."""

    config: dict
    status: str
    train_candles: int = 0
    test_candles: int = 0
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    equity_curve: list = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "config": self.config,
            "status": self.status,
            "train_candles": self.train_candles,
            "test_candles": self.test_candles,
            "total_return_pct": self.total_return_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "total_trades": self.total_trades,
            "equity_curve": self.equity_curve,
            "error": self.error,
        }


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


async def fetch_and_store_data(
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
    datastore: DataStore,
) -> int:
    """Fetch historical data from Bybit and store in SQLite."""
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

    saved = datastore.save_candles(candles)
    logger.info(f"Saved {saved} candles to database")
    return saved


async def run_single_experiment(
    config: ExperimentConfig,
    datastore: DataStore,
) -> ExperimentResult:
    """Run a single experiment: train on one period, test on next period."""

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Experiment: {config.symbol} {config.timeframe}")
    logger.info(f"Train: {config.train_start.date()} to {config.train_end.date()}")
    logger.info(f"Test: {config.test_start.date()} to {config.test_end.date()}")
    logger.info(f"Strategy: {config.strategy} | Epochs: {config.epochs}")
    logger.info(f"{'=' * 60}")

    result = ExperimentResult(
        config={
            "symbol": config.symbol,
            "timeframe": config.timeframe,
            "train_period": f"{config.train_start.date()} to {config.train_end.date()}",
            "test_period": f"{config.test_start.date()} to {config.test_end.date()}",
            "strategy": config.strategy,
            "epochs": config.epochs,
        },
        status="pending",
    )

    try:
        # Create strategy
        if config.strategy == "momentum":
            strategy = MomentumStrategy(MomentumConfig(name="momentum"))
        else:
            strategy = MeanReversionStrategy(MeanReversionConfig(name="mean_reversion"))

        # Get training data
        train_series = get_period_candles(
            datastore, config.symbol, config.timeframe, config.train_start, config.train_end
        )

        # Get test data
        test_series = get_period_candles(
            datastore, config.symbol, config.timeframe, config.test_start, config.test_end
        )

        logger.info(f"Training data: {len(train_series.candles)} candles")
        logger.info(f"Test data: {len(test_series.candles)} candles")

        result.train_candles = len(train_series.candles)
        result.test_candles = len(test_series.candles)

        if len(train_series.candles) < 100:
            result.status = "insufficient_train_data"
            return result

        if len(test_series.candles) < 50:
            result.status = "insufficient_test_data"
            return result

        # Create trading system
        ts_config = TradingSystemConfig(
            exchange="bybit",
            symbols=[config.symbol],
            timeframe=config.timeframe,
        )
        system = TradingSystem(ts_config)

        # Train model
        logger.info(f"Training on {config.train_start.date()} to {config.train_end.date()}...")
        train_results = system.train_model(train_series)

        # Run backtest on test period
        logger.info(
            f"Running backtest on {config.test_start.date()} to {config.test_end.date()}..."
        )
        backtest_result = await system.run_backtest_with_ml(
            strategy=strategy,
            candles=test_series,
            initial_capital=10000.0,
        )

        logger.info(
            f"Backtest complete: Return={backtest_result['total_return_pct']:.2f}%, "
            f"Sharpe={backtest_result['sharpe_ratio']:.2f}, Trades={backtest_result['total_trades']}"
        )

        result.status = "success"
        result.total_return_pct = backtest_result["total_return_pct"]
        result.sharpe_ratio = backtest_result["sharpe_ratio"]
        result.max_drawdown = backtest_result["max_drawdown"]
        result.win_rate = backtest_result["win_rate"]
        result.total_trades = backtest_result["total_trades"]

    except Exception as e:
        logger.error(f"Error in experiment: {e}")
        import traceback

        traceback.print_exc()
        result.status = "error"
        result.error = str(e)

    return result


async def ensure_data(
    datastore: DataStore,
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
) -> None:
    """Ensure data exists for the required period."""
    min_ts, max_ts = datastore.get_date_range(symbol, "bybit", timeframe)

    need_fetch = False
    if not min_ts or not max_ts:
        need_fetch = True
    else:
        existing_start = datetime.fromtimestamp(min_ts)
        existing_end = datetime.fromtimestamp(max_ts)
        if existing_start > start_date or existing_end < end_date:
            need_fetch = True

    if need_fetch:
        logger.info(f"Fetching missing data for {symbol} {timeframe}...")
        await fetch_and_store_data(symbol, timeframe, start_date, end_date, datastore)
    else:
        logger.info(f"Using existing data for {symbol} {timeframe}")


def print_summary(results: list[ExperimentResult]) -> None:
    """Print summary table of all results."""
    logger.info(f"\n{'=' * 120}")
    logger.info("EXPERIMENT RESULTS SUMMARY")
    logger.info(f"{'=' * 120}")

    # Group by category
    successful = [r for r in results if r.status == "success"]

    if not successful:
        logger.info("No successful experiments to display.")
        return

    # Sort by Sharpe ratio descending
    successful.sort(key=lambda x: x.sharpe_ratio, reverse=True)

    # Header
    logger.info(
        f"\n{'Symbol':<12} {'Strategy':<15} {'Train Period':<25} {'Test Period':<25} "
        f"{'Return%':<10} {'Sharpe':<8} {'WinRate':<8} {'Trades':<8}"
    )
    logger.info("-" * 120)

    for r in successful:
        logger.info(
            f"{r.config['symbol']:<12} {r.config['strategy']:<15} "
            f"{r.config['train_period']:<25} {r.config['test_period']:<25} "
            f"{r.total_return_pct:>8.2f}% {r.sharpe_ratio:>7.2f} {r.win_rate:>7.1%} {r.total_trades:>8}"
        )

    # Analysis by train period
    logger.info(f"\n{'=' * 80}")
    logger.info("ANALYSIS BY TRAINING PERIOD")
    logger.info(f"{'=' * 80}")

    period_groups = {}
    for r in successful:
        period = r.config["train_period"]
        if period not in period_groups:
            period_groups[period] = []
        period_groups[period].append(r)

    for period, group in sorted(period_groups.items(), key=lambda x: len(x[0])):
        avg_sharpe = sum(r.sharpe_ratio for r in group) / len(group)
        avg_return = sum(r.total_return_pct for r in group) / len(group)
        avg_trades = sum(r.total_trades for r in group) / len(group)
        logger.info(f"\n{period}:")
        logger.info(
            f"  Avg Sharpe: {avg_sharpe:.3f} | Avg Return: {avg_return:.2f}% | Avg Trades: {avg_trades:.1f}"
        )
        logger.info(f"  Assets tested: {[r.config['symbol'] for r in group]}")

    # Analysis by asset
    logger.info(f"\n{'=' * 80}")
    logger.info("ANALYSIS BY ASSET")
    logger.info(f"{'=' * 80}")

    asset_groups = {}
    for r in successful:
        asset = r.config["symbol"]
        if asset not in asset_groups:
            asset_groups[asset] = []
        asset_groups[asset].append(r)

    for asset, group in sorted(asset_groups.items()):
        avg_sharpe = sum(r.sharpe_ratio for r in group) / len(group)
        avg_return = sum(r.total_return_pct for r in group) / len(group)
        logger.info(f"\n{asset}:")
        logger.info(
            f"  Avg Sharpe: {avg_sharpe:.3f} | Avg Return: {avg_return:.2f}% | Count: {len(group)}"
        )

    # Best overall
    best = successful[0]
    logger.info(f"\n{'=' * 80}")
    logger.info("BEST OVERALL")
    logger.info(f"{'=' * 80}")
    logger.info(f"  Symbol: {best.config['symbol']}")
    logger.info(f"  Strategy: {best.config['strategy']}")
    logger.info(f"  Train Period: {best.config['train_period']}")
    logger.info(f"  Test Period: {best.config['test_period']}")
    logger.info(f"  Sharpe: {best.sharpe_ratio:.3f} | Return: {best.total_return_pct:.2f}%")
    logger.info(f"  Win Rate: {best.win_rate:.1%} | Trades: {best.total_trades}")


async def main():
    parser = argparse.ArgumentParser(
        description="Extended research for optimal train period and retrain frequency"
    )

    # Data range
    parser.add_argument(
        "--train-start", default="2025-11-01", help="Training start date (YYYY-MM-DD)"
    )
    parser.add_argument("--train-end", default="2025-12-31", help="Training end date (YYYY-MM-DD)")
    parser.add_argument("--test-start", default="2026-01-01", help="Test start date (YYYY-MM-DD)")
    parser.add_argument("--test-end", default="2026-01-31", help="Test end date (YYYY-MM-DD)")

    # Assets to test
    parser.add_argument(
        "--assets",
        nargs="+",
        default=["BTC-USDT"],
        help="Assets to test (e.g., BTC-USDT ETH-USDT DOGE-USDT TRX-USDT)",
    )

    # Timeframe
    parser.add_argument("--timeframe", default="5m", help="Candle timeframe (1m, 5m, 15m, 1h, 4h)")

    # Strategies
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["momentum"],
        help="Strategies to test (momentum, mean_reversion)",
    )

    # Epochs
    parser.add_argument("--epochs", type=int, default=20, help="Training epochs")

    # Output
    parser.add_argument(
        "--output", default="extended_research_results.json", help="Output file for results"
    )

    args = parser.parse_args()

    # Parse dates
    train_start = datetime.strptime(args.train_start, "%Y-%m-%d")
    train_end = datetime.strptime(args.train_end, "%Y-%m-%d")
    test_start = datetime.strptime(args.test_start, "%Y-%m-%d")
    test_end = datetime.strptime(args.test_end, "%Y-%m-%d")

    logger.info(f"\n{'=' * 80}")
    logger.info("EXTENDED RESEARCH: HYPERPARAMETER OPTIMIZATION")
    logger.info(f"{'=' * 80}")
    logger.info(f"Train period: {train_start.date()} to {train_end.date()}")
    logger.info(f"Test period: {test_start.date()} to {test_end.date()}")
    logger.info(f"Assets: {', '.join(args.assets)}")
    logger.info(f"Timeframe: {args.timeframe}")
    logger.info(f"Strategies: {', '.join(args.strategies)}")

    # Initialize datastore
    datastore = DataStore()

    # Ensure we have all required data
    all_assets = list(set(args.assets + ["BTC-USDT", "ETH-USDT", "DOGE-USDT", "TRX-USDT"]))
    data_end = test_end + timedelta(days=1)

    for asset in all_assets:
        symbol = asset.replace("-", "")  # e.g., BTC-USDT -> BTCUSDT
        await ensure_data(datastore, symbol, args.timeframe, train_start, data_end)

    # Build experiment configurations
    configs = []
    for asset, strategy in product(args.assets, args.strategies):
        symbol = asset.replace("-", "")
        config = ExperimentConfig(
            symbol=symbol,
            timeframe=args.timeframe,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            strategy=strategy,
            epochs=args.epochs,
        )
        configs.append(config)

    logger.info(f"\nRunning {len(configs)} experiments...")

    # Run experiments in parallel using asyncio.gather
    results = await asyncio.gather(
        *[run_single_experiment(config, datastore) for config in configs]
    )

    # Save results
    results_dict = [r.to_dict() for r in results]
    with open(args.output, "w") as f:
        json.dump(results_dict, f, indent=2, default=str)

    # Print summary
    print_summary(results)

    logger.info(f"\nResults saved to: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
