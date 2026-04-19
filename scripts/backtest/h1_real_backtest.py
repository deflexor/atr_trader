"""Real data backtest with trained H1Model.

Uses actual trained H1Model (models/h1_lstm_model.pt) on real 1m historical data.
Uses Bybit API for longer historical data (up to 200 candles * 1000 = 200k candles).

Usage:
    python scripts/backtest/h1_real_backtest.py --symbol BTCUSDT --days 90
"""

import asyncio
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np

from src.adapters.bybit_adapter import BybitAdapter
from src.core.db.datastore import DataStore
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def fetch_1m_candles_bybit(symbol: str, days: int = 90) -> CandleSeries:
    """Fetch 1m candles from Bybit with pagination."""
    adapter = BybitAdapter()
    
    # Bybit uses milliseconds for timestamps
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    
    logger.info(f"Fetching {days} days of 1m candles from Bybit...")
    
    # Bybit uses interval "1" for 1m candles
    raw_candles = await adapter.fetch_ohlcv_paginated(
        symbol, 
        timeframe="1",
        limit=1000,
        start_time=start_time,
        end_time=end_time,
    )
    
    candles = []
    for raw in raw_candles:
        try:
            # Bybit format: [startTime, open, high, low, close, volume, turnover]
            ts = int(raw[0]) // 1000  # Convert ms to seconds
            candles.append(Candle(
                symbol=symbol,
                exchange="bybit",
                timeframe="1m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(raw[1]),
                close=float(raw[4]),
                high=float(raw[2]),
                low=float(raw[3]),
                volume=float(raw[5]),
            ))
        except (ValueError, IndexError) as e:
            logger.debug(f"Skipping malformed candle: {e}")
            continue
    
    # Sort and dedupe
    candles.sort(key=lambda x: x.timestamp.timestamp())
    seen = {}
    result = []
    for c in candles:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    
    logger.info(f"Fetched {len(result)} unique 1m candles from Bybit")
    return CandleSeries(
        candles=result,
        symbol=symbol,
        exchange="bybit",
        timeframe="1m",
    )


def aggregate_to_1h(candles_1m: CandleSeries) -> CandleSeries:
    """Aggregate 1m candles to 1h candles."""
    if not candles_1m.candles:
        return CandleSeries(candles=[], symbol=candles_1m.symbol, exchange="bybit", timeframe="1h")
    
    h1_candles = []
    current_hour = []
    current_hour_start = None
    
    for candle in candles_1m.candles:
        hour_start = candle.timestamp.replace(minute=0, second=0, microsecond=0)
        
        if current_hour_start is None:
            current_hour_start = hour_start
            current_hour = [candle]
        elif hour_start == current_hour_start:
            current_hour.append(candle)
        else:
            # Close current hour and start new
            if len(current_hour) >= 30:  # At least 30 minutes of data
                h1_candles.append(Candle(
                    symbol=candles_1m.symbol,
                    exchange="bybit",
                    timeframe="1h",
                    timestamp=current_hour_start,
                    open=current_hour[0].open,
                    high=max(c.high for c in current_hour),
                    low=min(c.low for c in current_hour),
                    close=current_hour[-1].close,
                    volume=sum(c.volume for c in current_hour),
                ))
            current_hour_start = hour_start
            current_hour = [candle]
    
    # Don't forget last hour
    if len(current_hour) >= 30:
        h1_candles.append(Candle(
            symbol=candles_1m.symbol,
            exchange="bybit",
            timeframe="1h",
            timestamp=current_hour_start,
            open=current_hour[0].open,
            high=max(c.high for c in current_hour),
            low=min(c.low for c in current_hour),
            close=current_hour[-1].close,
            volume=sum(c.volume for c in current_hour),
        ))
    
    logger.info(f"Aggregated to {len(h1_candles)} 1h candles")
    return CandleSeries(
        candles=h1_candles,
        symbol=candles_1m.symbol,
        exchange="bybit",
        timeframe="1h",
    )


async def run_backtest_comparison(symbol: str, days: int = 90):
    """Run backtest with real data from Bybit."""
    
    # Fetch 1m data from Bybit
    print(f"\n[1/5] Fetching {days} days of 1m candles for {symbol} from Bybit...")
    candles_1m = await fetch_1m_candles_bybit(symbol, days=days)
    if len(candles_1m.candles) < 100:
        logger.error(f"Not enough candles: {len(candles_1m.candles)}")
        return None
    
    logger.info(f"Using {len(candles_1m.candles)} candles for backtest")
    
    # Create 1h candles for H1Model
    print("\n[2/5] Aggregating to 1h candles for H1Model...")
    h1_candles = aggregate_to_1h(candles_1m)
    
    # Load trained H1Model
    print("\n[3/5] Loading trained H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        logger.info("H1Model loaded from models/h1_lstm_model.pt")
    else:
        logger.warning("H1Model not found at models/h1_lstm_model.pt - using untagged")
    
    # Create strategy configs
    config_baseline = MomentumConfig(
        name="baseline",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0005,
        mtf_enabled=False,  # No MTF for baseline
    )
    strategy_baseline = MomentumStrategy(config=config_baseline)
    
    config_h1 = MomentumConfig(
        name="h1_enhanced",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0005,
        mtf_enabled=True,
    )
    strategy_h1 = MomentumStrategy(config=config_h1, h1_model=h1_model)
    
    # Run baseline backtest
    print("\n[4/5] Running baseline backtest (no H1Model)...")
    
    async def signal_gen_baseline(sym: str, c: CandleSeries):
        return await strategy_baseline.generate_signal(sym, c, None)
    
    engine = BacktestEngine(BacktestConfig(initial_capital=10000.0))
    result_baseline = await engine.run(candles_1m, signal_gen_baseline, 10000.0)
    
    print(f"   Baseline: {result_baseline.total_trades} trades, "
          f"win_rate={result_baseline.win_rate:.1%}, "
          f"return={result_baseline.total_return_pct:.2f}%, "
          f"drawdown={result_baseline.max_drawdown:.2f}%")
    
    # Run H1Model-enhanced backtest
    print("\n[5/5] Running H1Model-enhanced backtest...")
    
    async def signal_gen_h1(sym: str, c: CandleSeries):
        signal, mtf_info = await strategy_h1.multi_timeframe_signal(sym, c, None)
        return signal
    
    engine_h1 = BacktestEngine(BacktestConfig(initial_capital=10000.0))
    result_h1 = await engine_h1.run(candles_1m, signal_gen_h1, 10000.0)
    
    print(f"   H1Model: {result_h1.total_trades} trades, "
          f"win_rate={result_h1.win_rate:.1%}, "
          f"return={result_h1.total_return_pct:.2f}%, "
          f"drawdown={result_h1.max_drawdown:.2f}%")
    
    # Comparison
    print("\n" + "=" * 60)
    print("COMPARISON RESULTS")
    print("=" * 60)
    print(f"{'':20} {'Baseline':<12} {'H1Model':<12}")
    print("-" * 44)
    print(f"{'Total Trades':20} {result_baseline.total_trades:<12} {result_h1.total_trades:<12}")
    print(f"{'Win Rate':20} {result_baseline.win_rate:.1%}       {result_h1.win_rate:.1%}      ")
    print(f"{'Return %':20} {result_baseline.total_return_pct:.2f}%      {result_h1.total_return_pct:.2f}%      ")
    print(f"{'Max Drawdown %':20} {result_baseline.max_drawdown:.2f}%      {result_h1.max_drawdown:.2f}%      ")
    print(f"{'Sharpe Ratio':20} {result_baseline.sharpe_ratio:.2f}        {result_h1.sharpe_ratio:.2f}      ")
    print("-" * 44)
    
    # H1Model diagnostics
    print("\nH1Model Diagnostics:")
    for key, value in strategy_h1.diagnostics.items():
        if value > 0:
            print(f"   {key}: {value}")
    
    return {
        "baseline": result_baseline,
        "h1_enhanced": result_h1,
    }


async def main():
    parser = argparse.ArgumentParser(description="Real data H1Model backtest")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--days", type=int, default=90, help="Number of days of historical data")
    args = parser.parse_args()
    
    print("=" * 60)
    print("H1Model Real Data Backtest (Bybit)")
    print("=" * 60)
    print(f"Symbol:  {args.symbol}")
    print(f"Days:    {args.days}")
    print("=" * 60)
    
    results = await run_backtest_comparison(args.symbol, args.days)
    
    if results:
        # Save results
        results_file = Path("results") / f"h1_backtest_bybit_{args.symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        import json
        
        output = {
            "timestamp": datetime.now().isoformat(),
            "source": "bybit",
            "symbol": args.symbol,
            "days": args.days,
            "baseline": {
                "total_trades": results["baseline"].total_trades,
                "win_rate": results["baseline"].win_rate,
                "total_return_pct": results["baseline"].total_return_pct,
                "max_drawdown": results["baseline"].max_drawdown,
                "sharpe_ratio": results["baseline"].sharpe_ratio,
            },
            "h1_enhanced": {
                "total_trades": results["h1_enhanced"].total_trades,
                "win_rate": results["h1_enhanced"].win_rate,
                "total_return_pct": results["h1_enhanced"].total_return_pct,
                "max_drawdown": results["h1_enhanced"].max_drawdown,
                "sharpe_ratio": results["h1_enhanced"].sharpe_ratio,
            },
        }
        
        with open(results_file, 'w') as f:
            json.dump(output, f, indent=2)
        
        print(f"\nResults saved to {results_file}")


if __name__ == "__main__":
    asyncio.run(main())