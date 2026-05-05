"""Live trading entry point.

Usage: python run_live.py [--testnet] [--symbols BTCUSDT,ETHUSDT]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

import structlog

from src.live.trader import LiveTrader, LiveTradingConfig

logger = structlog.get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PyPSiK live trading")
    parser.add_argument("--testnet", action="store_true", help="Use Bybit testnet")
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated symbols (default from config)",
    )
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital")
    parser.add_argument("--risk", type=float, default=0.03, help="Risk per trade (fraction)")
    parser.add_argument("--max-positions", type=int, default=2, help="Max positions per symbol")
    return parser


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    args = build_parser().parse_args()

    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")

    if not api_key or not api_secret:
        print("ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set", file=sys.stderr)
        sys.exit(1)

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")]
        if args.symbols
        else None
    )

    config = LiveTradingConfig(
        api_key=api_key,
        api_secret=api_secret,
        testnet=args.testnet,
        symbols=symbols if symbols is not None else LiveTradingConfig.symbols,
        initial_capital=args.capital,
        risk_per_trade=args.risk,
        max_positions=args.max_positions,
    )

    logger.info(
        "live_trading.starting",
        symbols=config.symbols,
        capital=config.initial_capital,
        risk=config.risk_per_trade,
        max_positions=config.max_positions,
        testnet=args.testnet,
    )

    asyncio.run(_run(config))


async def _run(config: LiveTradingConfig) -> None:
    trader = LiveTrader(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(trader.stop()))

    try:
        await trader.start()
        await trader.run()
    except Exception as exc:
        logger.error("trader_crashed", error=str(exc))
    finally:
        await trader.stop()


if __name__ == "__main__":
    main()
