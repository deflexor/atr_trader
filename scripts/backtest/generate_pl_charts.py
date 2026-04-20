"""Generate P/L Charts for backtest results.

Usage: uv run python scripts/backtest/generate_pl_charts.py
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from src.adapters.bybit_adapter import BybitAdapter
from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


async def run_backtest_with_equity(symbol: str, days: int = 120, timeframe: str = "5") -> dict:
    """Run backtest and return equity curve data."""
    print(f"\nRunning backtest for {symbol}...")

    adapter = BybitAdapter()
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    raw = await adapter.fetch_ohlcv_paginated(symbol, timeframe, 1000, start_time, end_time)
    print(f"Got {len(raw)} candles")

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
    )
    engine = BacktestEngine(engine_config)

    async def signal_gen(sym, c):
        signal, mtf = await strategy.multi_timeframe_signal(sym, c, None)
        return signal

    backtest_result = await engine.run(candles, signal_gen, 10000.0)

    return {
        "symbol": symbol,
        "equity_curve": backtest_result.equity_curve,
        "trades": backtest_result.trades,
        "candles": candles,
        "final_capital": backtest_result.final_capital,
        "total_return_pct": backtest_result.total_return_pct,
        "engine": engine,
    }


def plot_equity_curve(ax, equity_curve, symbol, color):
    """Plot equity curve from backtest results."""
    if not equity_curve:
        return

    timestamps = [point["timestamp"] for point in equity_curve if "timestamp" in point]
    equity = [point["equity"] for point in equity_curve if "equity" in point]

    if timestamps and equity:
        ax.plot(timestamps, equity, label=symbol, color=color, linewidth=1.5)


def plot_trade_pnl_timeline(ax, equity_curve, trades, symbol, color):
    """Plot cumulative PnL from trades at equity curve timestamps."""
    if not trades or not equity_curve:
        return

    close_trades = [t for t in trades if t.get("side") == "close" and t.get("pnl") is not None]
    if not close_trades:
        return

    n_closes = len(close_trades)
    n_ec_points = len(equity_curve)

    cumulative_pnl = 0
    pnl_points = []

    for i, trade in enumerate(close_trades):
        cumulative_pnl += trade.get("pnl", 0)
        ec_idx = int((i + 1) * n_ec_points / n_closes) - 1
        ec_idx = max(0, min(ec_idx, n_ec_points - 1))
        ts = equity_curve[ec_idx]["timestamp"]
        pnl_points.append((ts, cumulative_pnl))

    if pnl_points:
        ts_list = [p[0] for p in pnl_points]
        pnl_list = [p[1] for p in pnl_points]
        ax.plot(ts_list, pnl_list, label=f"{symbol}", color=color, linewidth=2, linestyle="--", marker="o", markersize=4)


def plot_drawdown(ax, equity_curve, symbol, color):
    """Plot drawdown from equity curve."""
    if not equity_curve:
        return

    timestamps = []
    drawdowns = []

    peak = 0
    for point in equity_curve:
        equity = point.get("equity", 0)
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak * 100
            timestamps.append(point["timestamp"])
            drawdowns.append(drawdown)

    if timestamps:
        ax.fill_between(timestamps, 0, drawdowns, label=symbol, color=color, alpha=0.3)
        ax.plot(timestamps, drawdowns, color=color, linewidth=0.5)


def plot_price(ax, candles, symbol, color):
    """Plot normalized price for comparison."""
    if not candles.candles:
        return

    timestamps = [c.timestamp for c in candles.candles]
    closes = [c.close for c in candles.candles]

    if closes:
        base = closes[0]
        if base > 0:
            normalized = [(c / base) * 100 for c in closes]
        else:
            normalized = closes

        ax.plot(timestamps, normalized, label=f"{symbol}", color=color, linewidth=0.8, alpha=0.7)


async def main():
    symbols = ["DOGEUSDT", "TONUSDT"]
    days = 120
    colors = {"DOGEUSDT": "#2196F3", "TONUSDT": "#FF9800"}

    print(f"\n{'#'*60}")
    print(f"# P/L CHARTS FOR {', '.join(symbols)}")
    print(f"# Period: {days} days")
    print(f"{'#'*60}")

    results = {}
    for symbol in symbols:
        results[symbol] = await run_backtest_with_equity(symbol, days)
        ec = results[symbol]["equity_curve"]
        trades = results[symbol]["trades"]
        print(f"{symbol}: {len(ec)} equity points, {len(trades)} trades")

    # Create figure with 2x2 subplot grid
    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(f"P/L Analysis - {', '.join(symbols)} ({days}-day Backtest)", fontsize=14, fontweight="bold")

    # Plot 1: Equity Curves (top-left)
    ax1 = axes[0, 0]
    for symbol in symbols:
        plot_equity_curve(ax1, results[symbol]["equity_curve"], symbol, colors[symbol])
    ax1.set_title("Equity Curve ($)")
    ax1.set_ylabel("Equity ($)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax1.axhline(y=10000, color="black", linestyle="--", linewidth=0.8, alpha=0.5)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax1.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))

    # Plot 2: Cumulative Trade PnL (top-right)
    ax2 = axes[0, 1]
    for symbol in symbols:
        plot_trade_pnl_timeline(ax2, results[symbol]["equity_curve"], results[symbol]["trades"], symbol, colors[symbol])
    ax2.set_title("Cumulative Closed Trade PnL ($)")
    ax2.set_ylabel("PnL ($)")
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    ax2.axhline(y=0, color="black", linestyle="--", linewidth=0.8, alpha=0.5)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))

    # Plot 3: Drawdown (bottom-left)
    ax3 = axes[1, 0]
    for symbol in symbols:
        plot_drawdown(ax3, results[symbol]["equity_curve"], symbol, colors[symbol])
    ax3.set_title("Drawdown (%)")
    ax3.set_ylabel("Drawdown (%)")
    ax3.legend()
    ax3.grid(True, alpha=0.3)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax3.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))

    # Plot 4: Normalized Price Comparison (bottom-right)
    ax4 = axes[1, 1]
    for symbol in symbols:
        plot_price(ax4, results[symbol]["candles"], symbol, colors[symbol])
    ax4.set_title("Price (Normalized to 100)")
    ax4.set_ylabel("Normalized Price")
    ax4.legend()
    ax4.grid(True, alpha=0.3)
    ax4.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax4.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))

    plt.xticks(rotation=45)
    plt.tight_layout()

    # Save
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    Path("results").mkdir(exist_ok=True)
    output_file = Path(f"results/pl_charts_{date_str}.png")
    plt.savefig(output_file, dpi=150, bbox_inches="tight")
    print(f"\nChart saved to: {output_file}")

    pdf_file = Path(f"results/pl_charts_{date_str}.pdf")
    plt.savefig(pdf_file, bbox_inches="tight")
    print(f"PDF saved to: {pdf_file}")

    # Print summary with both closed and total returns
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for symbol in symbols:
        ec = results[symbol]["equity_curve"]
        trades = results[symbol]["trades"]
        engine = results[symbol]["engine"]

        closes = [t for t in trades if t.get("side") == "close"]
        winners = [t for t in closes if t.get("pnl", 0) > 0]
        losers = [t for t in closes if t.get("pnl", 0) <= 0]
        closed_pnl = sum(t.get("pnl", 0) for t in closes if t.get("pnl") is not None)

        initial_equity = ec[0]["equity"]
        final_equity = ec[-1]["equity"]
        equity_return = (final_equity - initial_equity) / initial_equity * 100

        # Calculate unrealized from engine's current positions
        last_price = results[symbol]["candles"].candles[-1].close
        unrealized = 0
        for p in engine.positions:
            if p.side == "long":
                unrealized += (last_price - p.avg_entry_price) * p.total_quantity
            else:
                unrealized += (p.avg_entry_price - last_price) * p.total_quantity

        total_pnl = closed_pnl + unrealized
        total_return = total_pnl / initial_equity * 100

        print(f"\n{symbol}:")
        print(f"  Initial: ${initial_equity:.2f}")
        print(f"  Final: ${final_equity:.2f}")
        print(f"  Equity Return: {equity_return:.2f}%")
        print(f"  Closed PnL: ${closed_pnl:.2f} ({len(winners)}W/{len(losers)}L)")
        print(f"  Unrealized PnL: ${unrealized:.2f} ({len(engine.positions)} open positions)")
        print(f"  Total PnL: ${total_pnl:.2f}")
        print(f"  Total Return: {total_return:.2f}%")

    plt.close()


if __name__ == "__main__":
    asyncio.run(main())