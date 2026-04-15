"""Example: Train ML model and run backtest on real historical data.

Workflow:
1. Fetch historical candles from exchange (KuCoin or Bybit)
2. Train LSTM neural net on historical data
3. Run backtest with ML-enhanced signal predictions
4. Evaluate performance with realistic slippage

Usage:
    python examples/ml_backtest.py --exchange kucoin --symbol BTCUSDT
"""

import asyncio
import argparse
import logging
from datetime import datetime

from src.trading_system import TradingSystem, quick_backtest, TradingSystemConfig
from src.strategies.momentum_strategy import MomentumStrategy, MomentumConfig
from src.strategies.mean_reversion_strategy import MeanReversionStrategy, MeanReversionConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(description="ML-enhanced backtest on real historical data")
    parser.add_argument(
        "--exchange", default="kucoin", choices=["kucoin", "bybit"], help="Exchange to use"
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair symbol")
    parser.add_argument("--timeframe", default="1m", help="Candle timeframe (1m, 5m, 15m)")
    parser.add_argument("--candles", type=int, default=1000, help="Number of historical candles")
    parser.add_argument(
        "--initial-capital", type=float, default=10000.0, help="Initial capital for backtest"
    )
    parser.add_argument(
        "--strategy",
        default="momentum",
        choices=["momentum", "mean_reversion"],
        help="Strategy to backtest",
    )
    parser.add_argument("--epochs", type=int, default=50, help="ML training epochs")
    args = parser.parse_args()

    print("=" * 60)
    print("ML-Enhanced Backtest")
    print("=" * 60)
    print(f"Exchange:    {args.exchange}")
    print(f"Symbol:      {args.symbol}")
    print(f"Timeframe:   {args.timeframe}")
    print(f"Candles:     {args.candles}")
    print(f"Strategy:    {args.strategy}")
    print(f"Epochs:      {args.epochs}")
    print(f"Capital:     ${args.initial_capital:,.2f}")
    print("=" * 60)

    # Create trading system
    config = TradingSystemConfig(
        exchange=args.exchange,
        symbols=[args.symbol],
        timeframe=args.timeframe,
        lookback_candles=args.candles,
    )
    system = TradingSystem(config)

    # Create strategy
    if args.strategy == "momentum":
        strategy = MomentumStrategy(
            MomentumConfig(
                name="momentum_backtest",
                rsi_period=14,
                rsi_overbought=70,
                rsi_oversold=30,
                momentum_threshold=0.02,
            )
        )
    else:
        strategy = MeanReversionStrategy(
            MeanReversionConfig(
                name="mean_reversion_backtest",
                sma_period=20,
                bollinger_period=20,
                bollinger_std=2.0,
            )
        )

    try:
        # Step 1: Fetch historical data
        print("\n[1/4] Fetching historical data from exchange...")
        candles = await system.fetch_historical_data(args.symbol, limit=args.candles)
        print(f"      ✓ Received {len(candles.candles)} candles")
        print(
            f"      Price range: ${candles.candles[0].close:.2f} - ${candles.candles[-1].close:.2f}"
        )

        # Step 2: Train ML model
        print(f"\n[2/4] Training LSTM model ({args.epochs} epochs)...")
        train_results = system.train_model(candles)
        print(f"      ✓ Model trained on {train_results['num_samples']} samples")
        final_loss = train_results["history"]["train_loss"][-1]
        print(f"      Final training loss: {final_loss:.4f}")

        # Step 3: Run ML-enhanced backtest
        print(f"\n[3/4] Running ML-enhanced backtest...")
        backtest_results = await system.run_backtest_with_ml(
            strategy=strategy,
            candles=candles,
            initial_capital=args.initial_capital,
        )

        # Step 4: Display results
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        print(f"Final Capital:    ${backtest_results['final_capital']:,.2f}")
        print(f"Total Return:      {backtest_results['total_return_pct']:.2f}%")
        print(f"Sharpe Ratio:      {backtest_results['sharpe_ratio']:.4f}")
        print(f"Max Drawdown:      ${backtest_results['max_drawdown']:,.2f}")
        print(f"Win Rate:          {backtest_results['win_rate']:.1%}")
        print(f"Total Trades:      {backtest_results['total_trades']}")
        print("=" * 60)

        # Interpretation
        print("\n📊 INTERPRETATION")
        print("-" * 60)
        if backtest_results["sharpe_ratio"] > 1.5:
            print("✅ Sharpe ratio > 1.5: Strategy shows strong risk-adjusted returns")
        elif backtest_results["sharpe_ratio"] > 1.0:
            print("⚠️  Sharpe ratio 1.0-1.5: Strategy is acceptable but room for improvement")
        else:
            print("❌ Sharpe ratio < 1.0: Strategy needs optimization or different approach")

        if backtest_results["max_drawdown"] > args.initial_capital * 0.2:
            print("⚠️  High max drawdown: Consider tighter stop losses or smaller position sizing")

        if backtest_results["win_rate"] > 0.5:
            print("✅ Win rate > 50%: Strategy has positive edge")
        else:
            print("⚠️  Win rate < 50%: Consider adjusting entry conditions")

        print("\n💡 NEXT STEPS")
        print("-" * 60)
        print("1. Try different strategy parameters")
        print("2. Increase training data (more candles)")
        print("3. Adjust feature engineering (window size, indicators)")
        print("4. Test on different symbols or timeframes")
        print("5. Add more strategies and run ensemble backtest")

    except Exception as e:
        logger.error(f"Error during backtest: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
