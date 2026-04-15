"""Diagnostic backtest: verify engine fixes with detailed trade logging.

Runs a short backtest and prints every trade to verify:
1. Close positions use current_price (not entry_price)
2. PnL is calculated correctly
3. Win rate is no longer 0%
4. ATR-based stops are working
5. Strategy-driven exits fire correctly
"""

import asyncio
import logging
from datetime import datetime, timedelta

from src.adapters.bybit_adapter import BybitAdapter
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.strategies.mean_reversion_strategy import MeanReversionStrategy, MeanReversionConfig
from src.core.db.datastore import DataStore
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.trading_system import TradingSystem, TradingSystemConfig

logging.basicConfig(
    level=logging.WARNING,  # Reduce noise
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def get_candles(datastore, symbol, timeframe, start_date, end_date):
    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())
    candles = datastore.get_candles(
        symbol=symbol,
        exchange="bybit",
        timeframe=timeframe,
        start_timestamp=start_ts,
        end_timestamp=end_ts,
    )
    return CandleSeries(candles=candles, symbol=symbol, exchange="bybit", timeframe=timeframe)


async def run_diagnostic():
    datastore = DataStore()

    # Use 2 weeks of backtest data for quick results
    start_date = datetime(2025, 12, 1)
    end_date = datetime(2025, 12, 15)

    series = get_candles(datastore, "BTCUSDT", "5m", start_date, end_date)
    print(f"Loaded {len(series.candles)} candles: {start_date.date()} to {end_date.date()}")

    if len(series.candles) < 100:
        print("ERROR: Not enough candles. Run research.py first to fetch data.")
        return

    # Print price range for context
    closes = series.closes
    print(f"Price range: ${min(closes):.2f} - ${max(closes):.2f}")

    # Strategy configs
    strategies = {
        "momentum": MomentumStrategy(MomentumConfig(name="momentum")),
        "mean_reversion": MeanReversionStrategy(MeanReversionConfig(name="mean_reversion")),
    }

    for strat_name, strategy in strategies.items():
        print(f"\n{'=' * 70}")
        print(f"STRATEGY: {strat_name}")
        print(f"{'=' * 70}")

        # Run without ML first (raw strategy)
        engine = BacktestEngine(BacktestConfig())

        # Simple signal generator (no ML, just strategy)
        async def raw_signal_generator(symbol, candles):
            return await strategy.generate_signal(symbol, candles)

        result = await engine.run(series, raw_signal_generator, initial_capital=10000.0)

        # Close remaining positions
        if engine.positions and series.candles:
            last_price = series.candles[-1].close
            last_volume = series.candles[-1].volume
            for pos in engine.positions[:]:
                engine._close_position(pos, last_price, last_volume, "end_of_backtest")

        # Recalculate with closed positions
        trades = engine.trades
        entries = [t for t in trades if t.get("side") in ("long", "short")]
        exits = [t for t in trades if t.get("side") == "close"]

        print(f"\nTotal trades: {len(trades)} ({len(entries)} entries, {len(exits)} exits)")
        print(f"Win Rate: {result.win_rate:.1%}")
        print(f"Return: {result.total_return_pct:.2f}%")
        print(f"Final Capital: ${result.final_capital:.2f}")

        # Print first 15 trades in detail
        print(f"\n--- First 15 Trades (detailed) ---")
        print(
            f"{'#':<4} {'Side':<7} {'Entry':>10} {'Exit':>10} {'Qty':>10} {'PnL':>10} {'PnL%':>8} {'Reason':<15}"
        )
        print("-" * 85)

        count = 0
        for t in trades:
            if t.get("side") == "close":
                count += 1
                if count > 15:
                    break
                entry = t.get("entry_price", 0)
                exit_p = t.get("exit_price", 0)
                pnl = t.get("pnl", 0)
                pnl_pct = t.get("pnl_pct", 0)
                reason = t.get("reason", "?")
                qty = t.get("quantity", 0)
                print(
                    f"{count:<4} {'close':<7} {entry:>10.2f} {exit_p:>10.2f} "
                    f"{qty:>10.6f} {pnl:>10.4f} {pnl_pct:>7.2f}% {reason:<15}"
                )

        # Show exit reason breakdown
        if exits:
            reasons = {}
            for e in exits:
                r = e.get("reason", "unknown")
                reasons[r] = reasons.get(r, 0) + 1

            print(f"\n--- Exit Reasons ---")
            for reason, count_r in sorted(reasons.items(), key=lambda x: -x[1]):
                pnl_for_reason = sum(e.get("pnl", 0) for e in exits if e.get("reason") == reason)
                print(f"  {reason:<20}: {count_r:>4} trades, total PnL: ${pnl_for_reason:.2f}")

        # Verify the fix: check that exit prices differ from entry prices
        stale_closes = [
            e for e in exits if abs(e.get("exit_price", 0) - e.get("entry_price", 0)) < 0.01
        ]
        if stale_closes:
            print(f"\n⚠️  WARNING: {len(stale_closes)} trades have exit_price ≈ entry_price!")
            print("  This suggests the _close_position bug is NOT fully fixed.")
        else:
            print(f"\n✅ All exit prices differ from entry prices (fix verified)")


if __name__ == "__main__":
    asyncio.run(run_diagnostic())
