"""Deep analysis of backtest issues."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.WARNING)


async def main():
    symbol = "BTCUSDT"
    days = 7

    print("Fetching data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, "1", 1000, start_time, end_time)

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

    seen = {}
    result = []
    for c in candles_list:
        ts = int(c.timestamp.timestamp())
        if ts not in seen:
            seen[ts] = c
            result.append(c)
    result.sort(key=lambda x: x.timestamp.timestamp())

    candles = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe="1m")
    print(f"Using {len(candles.candles)} candles")

    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))

    config = MomentumConfig(
        name="debug",
        min_agreement=2,
        pullback_enabled=True,
        volume_spike_threshold=1.5,
        atr_filter_min_pct=0.0002,
        mtf_enabled=True,
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.015,
        trailing_activation_atr=3.0,
        trailing_distance_atr=2.5,
        max_drawdown_pct=0.0,
        use_trailing_stop=True,
    )
    engine = BacktestEngine(engine_config)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    result = await engine.run(candles, signal_gen, 10000.0)

    print(f"\n{'='*60}")
    print("BASIC METRICS")
    print(f"{'='*60}")
    print(f"Initial Capital:  ${result.initial_capital:.2f}")
    print(f"Final Capital:    ${result.final_capital:.2f}")
    print(f"Total Return:     {result.total_return_pct:.2f}%")
    print(f"Max Drawdown:     {result.max_drawdown:.2f}%")

    # Analyze trades
    print(f"\n{'='*60}")
    print("TRADE ANALYSIS")
    print(f"{'='*60}")

    all_trades = result.trades
    print(f"Total trades recorded: {len(all_trades)}")

    # Separate entry and close trades
    entries = [t for t in all_trades if t.get("side") in ["long", "short"]]
    closes = [t for t in all_trades if t.get("side") == "close"]

    print(f"Entry trades: {len(entries)}")
    print(f"Close trades: {len(closes)}")

    # Calculate actual PnL from closed trades
    closed_pnls = [t.get("pnl", 0) for t in closes if t.get("pnl") is not None]
    if closed_pnls:
        winners = [p for p in closed_pnls if p > 0]
        losers = [p for p in closed_pnls if p <= 0]
        actual_win_rate = len(winners) / len(closed_pnls)

        print(f"\nClosed trade PnLs: {len(closed_pnls)}")
        print(f"Winners: {len(winners)} ({len(winners)/len(closed_pnls)*100:.1f}%)")
        print(f"Losers: {len(losers)} ({len(losers)/len(closed_pnls)*100:.1f}%)")
        print(f"Actual Win Rate: {actual_win_rate:.1%}")
        print(f"Total PnL: ${sum(closed_pnls):.4f}")
        if winners:
            print(f"Avg Win: ${sum(winners)/len(winners):.4f}")
        if losers:
            print(f"Avg Loss: ${sum(losers)/len(losers):.4f}")

    print(f"\nReported Win Rate: {result.win_rate:.1%}")
    print(f"Reported Total Trades: {result.total_trades}")

    # Analyze position sizing
    print(f"\n{'='*60}")
    print("POSITION SIZING ANALYSIS")
    print(f"{'='*60}")
    for i, trade in enumerate(entries[:5]):
        entry_price = trade.get("entry_price", 0)
        quantity = trade.get("quantity", 0)
        position_value = entry_price * quantity if entry_price and quantity else 0
        risk_pct = position_value / 10000 * 100 if position_value else 0
        print(f"Trade {i+1}: entry=${entry_price:.2f} qty={quantity:.6f} value=${position_value:.2f} ({risk_pct:.2f}% of capital)")

    # Equity curve
    print(f"\n{'='*60}")
    print("EQUITY CURVE")
    print(f"{'='*60}")
    equity = [eq["equity"] for eq in result.equity_curve]
    print(f"Start: ${equity[0]:.2f}")
    print(f"Min:   ${min(equity):.2f} ({(min(equity)-equity[0])/equity[0]*100:.2f}%)")
    print(f"Max:   ${max(equity):.2f} ({max(equity)/equity[0]*100 - 100:.2f}%)")
    print(f"End:   ${equity[-1]:.2f} ({equity[-1]/equity[0]*100 - 100:.2f}%)")

    # Check the max drawdown calculation
    peak = equity[0]
    max_dd = 0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    print(f"\nManual max drawdown calc: ${max_dd:.2f} ({max_dd/peak*100:.2f}%)")
    print(f"Backtest reports: {result.max_drawdown:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())