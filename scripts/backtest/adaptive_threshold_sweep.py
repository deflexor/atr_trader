"""Quick adaptive sizing sweep: test different threshold configs."""
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.core.models.candle import Candle, CandleSeries
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.ml.h1_model import H1Model
from src.risk.regime_detector import MarketRegime


def load_candles(symbol, timeframe, start_ts, end_ts):
    conn = sqlite3.connect("data/candles.db")
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT timestamp, open, high, low, close, volume "
        "FROM candles WHERE symbol=? AND timeframe=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp",
        (symbol, timeframe, start_ts, end_ts),
    ).fetchall()
    conn.close()
    candles_list = []
    for ts, o, h, l, c, v in rows:
        candles_list.append(Candle(
            symbol=symbol, exchange="bybit", timeframe=timeframe + "m",
            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
            open=float(o), high=float(h), low=float(l),
            close=float(c), volume=float(v),
        ))
    return CandleSeries(candles_list, symbol=symbol, exchange="bybit", timeframe=timeframe + "m")


def summarize(label, result, engine):
    closes = [t for t in result.trades if t.get("side") == "close"]
    partials = [t for t in result.trades if t.get("side") == "partial_close"]
    winners = [t for t in closes if t.get("pnl", 0) > 0]
    partial_pnl = sum(t["pnl"] for t in partials) if partials else 0
    print(f"  {label}")
    print(f"    Return: {result.total_return_pct:+.2f}% | MaxDD: {result.max_drawdown:.2f}% | "
          f"WR: {result.win_rate:.1%} | Sharpe: {result.sharpe_ratio:.2f}")
    print(f"    Trades: {result.total_trades} | Closes: {len(closes)} | Partials: {len(partials)}")
    if partials:
        print(f"    Partial PnL: ${partial_pnl:.2f}")
    ad = engine._risk_filter_stats.get("adaptive_reduced", 0)
    if ad:
        print(f"    adaptive_reduced: {ad}")


async def run_one(candles, cfg, h1_model, label):
    strategy = MomentumStrategy(config=MomentumConfig(
        name="test", min_agreement=2, pullback_enabled=False,
        volume_spike_threshold=1.0, atr_filter_min_pct=0.00005, mtf_enabled=True,
    ), h1_model=h1_model)
    engine = BacktestEngine(cfg)

    async def gen(sym, c):
        sig, _ = await strategy.multi_timeframe_signal(sym, c, None)
        return sig

    result = await engine.run(candles, gen, 10000.0)
    summarize(label, result, engine)


async def main():
    # Use 7 days covering the worst crash (Feb 5-6)
    start = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 2, 10, tzinfo=timezone.utc).timestamp())
    candles = load_candles("BTCUSDT", "5m", start, end)
    print(f"Loaded {len(candles.candles)} candles (Jan 15 - Feb 28, 2026)")

    h1_model = H1Model()
    if Path("models/h1_lstm_model.pt").exists():
        h1_model.load("models/h1_lstm_model.pt")

    base = dict(
        initial_capital=10000.0, risk_per_trade=0.03,
        trailing_activation_atr=8.0, trailing_distance_atr=4.0,
        max_drawdown_pct=0.20, use_trailing_stop=True, cooldown_candles=6,
        use_zero_drawdown_layer=True, regime_lookback=100, boltzmann_temperature=0.5,
        bootstrap_stops_enabled=False, per_trade_drawdown_budget=0.05,
        total_drawdown_budget=0.20,
    )

    await run_one(candles, BacktestConfig(**base, use_adaptive_sizing=False),
                  h1_model, "PHASE 1 (no adaptive)")

    # Regime-aware defaults (CRASH + VOLATILE_TRENDING only)
    await run_one(candles, BacktestConfig(**base, use_adaptive_sizing=True),
                  h1_model, "PHASE 2 (regime-aware defaults)")


if __name__ == "__main__":
    asyncio.run(main())
