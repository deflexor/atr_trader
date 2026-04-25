"""Run 90-day backtest with BEST enhanced config across all 8 assets.

Best config from Phase 6: min_agreement=3, breakout_lookback=100,
bollinger_required=True, 3% risk, 8h cooldown.
"""

import asyncio
import time
import sys
from src.strategies.enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.core.models.candle import CandleSeries
from src.core.db.datastore import DataStore
from datetime import datetime


async def main():
    ds = DataStore()
    symbols = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "TRXUSDT",
               "SOLUSDT", "ADAUSDT", "AVAXUSDT", "UNIUSDT"]
    days = 90

    # BEST config from Phase 6 (proven +4.64%/month on 4 assets)
    enh_cfg = EnhancedSignalConfig(
        min_agreement=3, rsi_oversold=25.0, rsi_overbought=75.0,
        bollinger_required=True, breakout_lookback=100,
        breakout_min_range_pct=0.002, breakout_strength=0.8,
        mean_reversion_strength=0.7, trend_strength=0.8,
        vwap_enabled=False,  # VWAP didn't add alpha
        divergence_enabled=False,  # Divergence added noise
    )

    bt_cfg = BacktestConfig(
        initial_capital=1250.0,  # 10k / 8 = 1250 each
        risk_per_trade=0.03, max_positions=2,
        cooldown_candles=96, use_trailing_stop=True,
        trailing_activation_atr=2.0, trailing_distance_atr=1.5,
        use_atr_stops=True, use_zero_drawdown_layer=False,
    )

    total_return = 0.0
    total_trades = 0
    results = {}

    for sym in symbols:
        raw = ds.get_candles(sym, "bybit", "5m")
        if not raw:
            print(f"{sym}: NO DATA")
            continue
        latest_ts = raw[-1].timestamp.timestamp() if isinstance(raw[-1].timestamp, datetime) else raw[-1].timestamp
        cutoff = latest_ts - days * 86400
        filtered = [c for c in raw if (c.timestamp.timestamp() if isinstance(c.timestamp, datetime) else c.timestamp) >= cutoff]
        candles = CandleSeries(candles=filtered, symbol=sym, exchange="bybit", timeframe="5m")

        async def enh_gen(symbol, cs):
            return generate_enhanced_signal(symbol, cs, enh_cfg)

        engine = BacktestEngine(bt_cfg)
        t0 = time.time()
        result = await engine.run(candles, enh_gen)
        elapsed = time.time() - t0

        trades = result.trades or []
        ret = result.total_return_pct
        total_return += ret
        total_trades += len(trades)
        results[sym] = ret

        print(f"{sym}: {len(trades)} trades, {ret:+.2f}%, {elapsed:.0f}s")
        sys.stdout.flush()

    monthly = total_return / 3
    print(f"\n=== 8-Asset Best Config (90d) ===")
    print(f"Total return: {total_return:+.2f}%")
    print(f"Monthly estimate: {monthly:+.2f}%")
    print(f"Total trades: {total_trades}")
    print(f"Per-asset avg: {total_return/len(results):+.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
