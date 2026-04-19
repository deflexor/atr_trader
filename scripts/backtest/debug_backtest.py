"""Debug 30-day backtest to understand low win rate and high drawdown."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


async def main():
    symbol = "BTCUSDT"
    days = 7  # Quick 7-day test first

    # 1. Fetch data
    print(f"Fetching {days} days of data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)
    print(f"Got {len(raw)} candles")

    # Parse candles
    candles_list = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles_list.append(Candle(
                symbol=symbol, exchange="bybit", timeframe="1m",
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5])
            ))
        except:
            continue

    # Deduplicate
    seen = {}
    result = []
    for c in candles_list:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    result.sort(key=lambda x: x.timestamp.timestamp())

    candles = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1m")
    print(f"Using {len(candles.candles)} 1m candles")

    # 2. Setup strategy with best config
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")

    config = MomentumConfig(
        name="debug",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    # 3. Run backtest
    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        trailing_activation_atr=3.0,
        trailing_distance_atr=2.5,
        max_drawdown_pct=0.0,  # Disable halt for debug
    )
    engine = BacktestEngine(engine_config)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    result = await engine.run(candles, signal_gen, 10000.0)

    # 4. Print detailed analysis
    print(f"\n{'='*60}")
    print("BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"Total Trades:     {result.total_trades}")
    print(f"Win Rate:        {result.win_rate:.1%}")
    print(f"Return:          {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:    {result.max_drawdown:.2f}%")
    print(f"Final Capital:   ${result.final_capital:.2f}")

    print(f"\n{'='*60}")
    print("DIAGNOSTICS")
    print(f"{'='*60}")
    for k, v in strategy.diagnostics.items():
        if v > 0:
            print(f"  {k}: {v}")

    print(f"\n{'='*60}")
    print("TRADES (closed positions only)")
    print(f"{'='*60}")
    closed_trades = [t for t in result.trades if t.get("side") == "close"]
    for i, trade in enumerate(closed_trades[:20]):  # First 20 trades
        pnl = trade.get("pnl", 0)
        pnl_pct = trade.get("pnl_pct", 0)
        entry = trade.get("entry_price", 0)
        exit = trade.get("exit_price", 0)
        reason = trade.get("reason", "?")
        print(f"  Trade {i+1}: entry=${entry:.2f} exit=${exit:.2f} pnl=${pnl:.2f} ({pnl_pct:.2f}%) [{reason}]")

    # Show ALL trades including entries
    print(f"\nALL TRADES recorded ({len(result.trades)}):")
    for i, trade in enumerate(result.trades[:30]):
        side = trade.get("side", "?")
        entry = trade.get("entry_price", 0)
        exit = trade.get("exit_price", 0)
        pnl = trade.get("pnl", 0)
        reason = trade.get("reason", "?")
        print(f"  {i+1}: {side} entry=${entry:.2f} exit=${exit:.2f} pnl=${pnl:.2f} [{reason}]")

    # 5. Analyze equity curve
    print(f"\n{'='*60}")
    print("EQUITY CURVE (first/last 5)")
    print(f"{'='*60}")
    for i, eq in enumerate(result.equity_curve[:5]):
        print(f"  {i}: ${eq['equity']:.2f}")
    print("  ...")
    for i, eq in enumerate(result.equity_curve[-5:], len(result.equity_curve)-5):
        print(f"  {i}: ${eq['equity']:.2f}")

    # 6. Check ATR values
    print(f"\n{'='*60}")
    print("ANALYSIS: Why so few trades?")
    print(f"{'='*60}")
    total = strategy.diagnostics.get("total_evaluated", 1)
    filters = {
        "atr_filtered": strategy.diagnostics.get("atr_filtered", 0),
        "volume_filtered": strategy.diagnostics.get("volume_filtered", 0),
        "pullback_filtered": strategy.diagnostics.get("pullback_filtered", 0),
        "h1_model_filtered": strategy.diagnostics.get("h1_model_filtered", 0),
        "rsi_divergence_filtered": strategy.diagnostics.get("rsi_divergence_filtered", 0),
    }
    for name, count in filters.items():
        if count > 0:
            print(f"  {name}: {count} ({count/total*100:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())