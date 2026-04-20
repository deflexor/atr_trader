"""Multi-Asset Backtest - 120 Days on 5m Timeframe

Runs backtest on multiple assets with same settings:
- 120 days of 5m data
- Balanced settings (3% risk, 8 ATR trailing, no fixed TP)
- Uses existing H1Model for trend confirmation

Usage: uv run python scripts/backtest/multi_asset_backtest.py [SYMBOL] [DAYS]
Example: uv run python scripts/backtest/multi_asset_backtest.py TRXUSDT 120
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


async def run_backtest(symbol: str, days: int = 120, timeframe: str = "5") -> dict:
    """Run backtest for a single asset."""
    print(f"\n{'='*60}")
    print(f"ASSET: {symbol} | {days} days | {timeframe}m")
    print(f"{'='*60}")

    print(f"Fetching {days} days of {symbol} {timeframe}m data...")
    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)
    print(f"Got {len(raw)} {timeframe}m candles")

    if len(raw) < 100:
        print(f"WARNING: Only {len(raw)} candles fetched - may be insufficient data")
        return None

    candles_list = []
    for r in raw:
        try:
            ts = int(r[0]) // 1000
            candles_list.append(Candle(
                symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m",
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

    candles = CandleSeries(result, symbol=symbol, exchange="bybit", timeframe=f"{timeframe}m")
    print(f"Using {len(candles.candles)} candles")

    # Load H1Model
    h1_model = H1Model()
    model_path = Path("models/h1_lstm_model.pt")
    if model_path.exists():
        h1_model.load(str(model_path))
        print("H1Model loaded")
    else:
        print("WARNING: H1Model not found - trend confirmation disabled")

    # BALANCED config
    config = MomentumConfig(
        name=f"balanced_{days}day_{timeframe}m",
        min_agreement=2,
        pullback_enabled=False,
        volume_spike_threshold=1.0,
        atr_filter_min_pct=0.00005,
        mtf_enabled=model_path.exists(),
    )
    strategy = MomentumStrategy(config=config, h1_model=h1_model)

    engine_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade=0.03,
        trailing_activation_atr=8.0,
        trailing_distance_atr=4.0,
        max_drawdown_pct=0.20,
        use_trailing_stop=True,
        use_atr_stops=False,
        cooldown_candles=6,
        volatility_adjustment_enabled=config.volatility_adjustment_enabled,
        volatility_lookback=config.volatility_lookback,
    )

    # Create Kelly sizer if geometric sizing enabled
    kelly_sizer = None
    if config.use_geometric_sizing:
        from src.strategies.momentum_strategy import KellyPositionSizer
        kelly_sizer = KellyPositionSizer(
            max_kelly_pct=config.kelly_max_pct,
            kelly_fraction=config.kelly_fraction,
            min_kelly_fraction=config.min_kelly_fraction,
        )
        engine_config.use_geometric_sizing = True

    engine = BacktestEngine(engine_config, kelly_sizer=kelly_sizer)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    start = datetime.now()
    backtest_result = await engine.run(candles, signal_gen, 10000.0)
    duration = (datetime.now() - start).total_seconds()

    # Parse trades
    closes = [t for t in backtest_result.trades if t.get("side") == "close"]
    winners = [t for t in closes if t.get("pnl", 0) > 0]
    losers = [t for t in closes if t.get("pnl", 0) <= 0]

    # Unrealized PnL - use engine.positions (NOT entries which includes pyramid entries)
    last_price = candles.candles[-1].close
    open_pnl = 0
    for pos in engine.positions:
        if pos.side == "long":
            open_pnl += (last_price - pos.avg_entry_price) * pos.total_quantity
        else:
            open_pnl += (pos.avg_entry_price - last_price) * pos.total_quantity

    num_open_positions = len(engine.positions)

    closed_pnl = sum(t.get("pnl", 0) for t in closes if t.get("pnl") is not None)
    total_pnl = closed_pnl + open_pnl
    total_return = total_pnl / 10000 * 100

    # Close reasons
    reasons = {}
    for t in closes:
        reason = t.get("reason", "?")
        reasons[reason] = reasons.get(reason, 0) + 1

    # Print summary
    print(f"\nDuration:       {duration:.1f}s")
    print(f"Initial:       ${backtest_result.initial_capital:.2f}")
    print(f"Final:         ${backtest_result.final_capital:.2f}")
    print(f"Return:        {backtest_result.total_return_pct:.2f}%")
    print(f"Max Drawdown:  {backtest_result.max_drawdown:.2f}%")
    print(f"Sharpe:        {backtest_result.sharpe_ratio:.2f}")
    print(f"Total Trades:  {backtest_result.total_trades}")
    print(f"Win Rate:      {backtest_result.win_rate:.1%}")
    print(f"Closed Trades: {len(closes)} | Winners: {len(winners)} | Losers: {len(losers)}")
    print(f"Avg Win:       ${sum(t.get('pnl', 0) for t in winners)/len(winners) if winners else 0:.2f}")
    print(f"Avg Loss:      ${sum(t.get('pnl', 0) for t in losers)/len(losers) if losers else 0:.2f}")
    print(f"Open Positions: {num_open_positions} | Unrealized: ${open_pnl:.2f}")
    print(f"TOTAL RETURN:  {total_return:.2f}%")

    return {
        "symbol": symbol,
        "days": days,
        "timeframe": timeframe,
        "candles": len(candles.candles),
        "duration_s": duration,
        "initial_capital": backtest_result.initial_capital,
        "final_capital": backtest_result.final_capital,
        "return_pct": backtest_result.total_return_pct,
        "total_return": total_return,
        "max_drawdown": backtest_result.max_drawdown,
        "sharpe": backtest_result.sharpe_ratio,
        "total_trades": backtest_result.total_trades,
        "win_rate": backtest_result.win_rate,
        "closed_trades": len(closes),
        "winners": len(winners),
        "losers": len(losers),
        "avg_win": sum(t.get("pnl", 0) for t in winners)/len(winners) if winners else 0,
        "avg_loss": sum(t.get("pnl", 0) for t in losers)/len(losers) if losers else 0,
        "open_positions": num_open_positions,
        "unrealized_pnl": open_pnl,
        "closed_pnl": closed_pnl,
        "total_pnl": total_pnl,
        "close_reasons": reasons,
    }


async def main():
    # Default assets
    assets = ["BTCUSDT", "TRXUSDT", "DOGEUSDT", "XMRUSDT"]
    days = 120

    # Parse command line args
    if len(sys.argv) > 1:
        assets = [sys.argv[1]]
    if len(sys.argv) > 2:
        days = int(sys.argv[2])

    print(f"\n{'#'*60}")
    print(f"# MULTI-ASSET BACKTEST")
    print(f"# Assets: {', '.join(assets)}")
    print(f"# Period: {days} days")
    print(f"# Timeframe: 5m")
    print(f"{'#'*60}")

    results = []
    for symbol in assets:
        result = await run_backtest(symbol, days)
        if result:
            results.append(result)

    # Summary table
    print(f"\n{'='*80}")
    print("SUMMARY - ALL ASSETS")
    print(f"{'='*80}")
    print(f"{'Symbol':<12} {'Trades':>8} {'Win%':>8} {'Return%':>10} {'MaxDD%':>8} {'Sharpe':>10}")
    print(f"{'-'*80}")
    for r in results:
        print(f"{r['symbol']:<12} {r['total_trades']:>8} {r['win_rate']*100:>7.1f}% {r['total_return']:>9.2f}% {r['max_drawdown']:>7.2f}% {r['sharpe']:>10.0f}")

    # Save consolidated report
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    Path("results").mkdir(exist_ok=True)
    output_file = Path(f"results/multi_asset_{days}day_{date_str}.md")

    report = f"""# Multi-Asset Backtest Results

**Date**: {date_str}
**Period**: {days} days
**Timeframe**: 5m
**Assets**: {', '.join([r['symbol'] for r in results])}

## Summary

| Symbol | Candles | Duration | Trades | Win Rate | Return | Max DD | Sharpe |
|--------|---------|----------|--------|----------|--------|--------|--------|
"""
    for r in results:
        report += f"| {r['symbol']} | {r['candles']} | {r['duration_s']:.1f}s | {r['total_trades']} | {r['win_rate']*100:.1f}% | {r['total_return']:.2f}% | {r['max_drawdown']:.2f}% | {r['sharpe']:.0f} |\n"

    report += f"""
## Detailed Results

"""
    for r in results:
        report += f"""### {r['symbol']}

**Performance**:
- Initial: ${r['initial_capital']:.2f}
- Final: ${r['final_capital']:.2f}
- Total Return: {r['total_return']:.2f}%
- Max Drawdown: {r['max_drawdown']:.2f}%
- Sharpe: {r['sharpe']:.0f}

**Trades**:
- Total: {r['total_trades']}
- Closed: {r['closed_trades']}
- Winners: {r['winners']}
- Losers: {r['losers']}
- Win Rate: {r['win_rate']*100:.1f}%
- Avg Win: ${r['avg_win']:.2f}
- Avg Loss: ${r['avg_loss']:.2f}

**Positions**:
- Open: {r['open_positions']}
- Unrealized PnL: ${r['unrealized_pnl']:.2f}
- Closed PnL: ${r['closed_pnl']:.2f}
- Total PnL: ${r['total_pnl']:.2f}

**Close Reasons**:
"""
        for reason, count in sorted(r['close_reasons'].items(), key=lambda x: -x[1]):
            report += f"- {reason}: {count}\n"
        report += "\n"

    output_file.write_text(report)
    print(f"\nResults saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())