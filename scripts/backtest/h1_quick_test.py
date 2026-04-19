"""Quick H1Model test with real Bybit data.

Usage: uv run python scripts/backtest/h1_quick_test.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


async def main():
    symbol = "BTCUSDT"
    days = 7
    
    # 1. Fetch 1m candles from Bybit
    print(f"Fetching {days} days of 1m candles from Bybit...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
    
    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)
    
    candles = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles.append(Candle(
                symbol=symbol, exchange="bybit", timeframe="1m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])
            ))
        except:
            continue
    
    candles.sort(key=lambda x: x.timestamp.timestamp())
    seen = {}
    result = []
    for c in candles:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    
    candles_1m = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1m")
    print(f"Got {len(candles_1m.candles)} 1m candles")
    
    # 2. Load H1Model
    print("Loading H1Model...")
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")
    else:
        print("WARNING: H1Model not found!")
    
    # 3. Run backtest with H1Model
    print("\nRunning backtest...")
    config_h1 = MomentumConfig(
        name="h1_test",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,  # Lower threshold for more signals
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config_h1, h1_model=h1_model)
    
    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal
    
    engine = BacktestEngine(BacktestConfig(initial_capital=10000.0))
    result = await engine.run(candles_1m, signal_gen, 10000.0)
    
    print(f"\n{'='*50}")
    print("RESULTS")
    print(f"{'='*50}")
    print(f"Total Trades:  {result.total_trades}")
    print(f"Win Rate:     {result.win_rate:.1%}")
    print(f"Return:       {result.total_return_pct:.2f}%")
    print(f"Max Drawdown: {result.max_drawdown:.2f}%")
    print(f"Sharpe:       {result.sharpe_ratio:.2f}")
    print(f"\nDiagnostics:")
    for k, v in strategy.diagnostics.items():
        if v > 0:
            print(f"  {k}: {v}")

if __name__ == "__main__":
    asyncio.run(main())