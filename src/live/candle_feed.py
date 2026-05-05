"""Provides 5m candle data for live trading."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog

from ..core.models.candle import Candle, CandleSeries

logger = structlog.get_logger(__name__)


def _ohlcv_to_candle(row: list, symbol: str, timeframe: str) -> Candle:
    """Convert a ccxt OHLCV row [ts_ms, o, h, l, c, v] to a Candle."""
    ts_ms, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
    return Candle(
        symbol=symbol,
        exchange="bybit",
        timeframe=timeframe,
        timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
    )


class CandleFeed:
    """Provides 5m candle data for live trading.

    Uses ExchangeClient (ccxt) for REST OHLCV fetching.
    Maintains a rolling window of candles per symbol for signal generation.
    Supports resume from last known timestamp via StateManager.
    """

    def __init__(
        self,
        exchange_client,  # ExchangeClient — ccxt wrapper
        state_manager,    # StateManager — resume timestamp
        symbols: list[str],
        timeframe: str = "5m",
        lookback_candles: int = 200,
        market_type: str = "perp",
    ) -> None:
        self._exchange = exchange_client
        self._state = state_manager
        self._symbols = symbols
        self._timeframe = timeframe
        self._lookback = lookback_candles
        self._market_type = market_type
        self._buffers: dict[str, list[Candle]] = {}
        self._series: dict[str, CandleSeries] = {}
        self._log = logger.bind(component="candle_feed")

    # ── lifecycle ──────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load historical candles for each symbol.

        For each symbol:
        1. Check state_manager for a resume timestamp
        2. If found, fetch candles from that timestamp onward
        3. Otherwise, fetch the last lookback_candles
        4. Build per-symbol CandleSeries
        """
        for symbol in self._symbols:
            try:
                candles = await self._load_historical(symbol)
                self._buffers[symbol] = candles
                self._series[symbol] = self._build_series(symbol, candles)
                self._log.info(
                    "initialized",
                    symbol=symbol,
                    candles=len(candles),
                )
            except Exception as exc:
                self._log.warning("init_failed", symbol=symbol, error=str(exc))
                self._buffers[symbol] = []
                self._series[symbol] = self._build_series(symbol, [])

    async def _load_historical(self, symbol: str) -> list[Candle]:
        """Fetch historical candles, resuming from last known timestamp."""
        resume_ts = await self._state.get_state(f"last_candle_{symbol}")

        if resume_ts:
            since = int(
                datetime.fromisoformat(resume_ts).timestamp() * 1000
            )
            rows = await self._fetch_ohlcv(symbol, since=since)
        else:
            rows = await self._fetch_ohlcv(symbol)

        return [_ohlcv_to_candle(r, symbol, self._timeframe) for r in rows]

    async def _fetch_ohlcv(
        self,
        symbol: str,
        since: Optional[int] = None,
    ) -> list[list]:
        """Call ccxt fetch_ohlcv through the ExchangeClient."""
        exchange = self._exchange._exchange  # ccxt instance
        ccxt_symbol = _normalize(symbol, self._market_type)
        return await exchange.fetch_ohlcv(
            ccxt_symbol,
            self._timeframe,
            since=since,
            limit=self._lookback,
        )

    @staticmethod
    def _normalize(symbol: str, market_type: str = "perp") -> str:
        """Convert raw symbol to ccxt format: BTCUSDT -> BTC/USDT:USDT."""
        clean = symbol.upper().strip()
        if ":" in clean:
            return clean
        base = clean.split("/")[0] if "/" in clean else clean.rstrip("USDT")
        if market_type == "perp":
            return f"{base}/USDT:USDT"
        return f"{base}/USDT"

    # ── live updates ───────────────────────────────────────────

    async def fetch_latest_candle(self, symbol: str) -> Optional[Candle]:
        """Fetch the most recent completed candle for a symbol.

        Fetches the last 2 OHLCV rows; the second-to-last is the
        last *completed* candle (the final row may be still forming).
        Returns None on error (logs warning, never raises).
        """
        try:
            exchange = self._exchange._exchange
            ccxt_symbol = self._normalize(symbol, self._market_type)
            rows = await exchange.fetch_ohlcv(
                ccxt_symbol, self._timeframe, limit=2,
            )
            if not rows:
                return None
            # Use second-to-last (last fully closed candle)
            idx = -2 if len(rows) >= 2 else -1
            return _ohlcv_to_candle(rows[idx], symbol, self._timeframe)
        except Exception as exc:
            self._log.warning("fetch_failed", symbol=symbol, error=str(exc))
            return None

    async def update_candles(self, symbol: str) -> Optional[CandleSeries]:
        """Fetch latest candle and update the rolling buffer if new.

        1. Fetch latest completed candle
        2. If its timestamp is newer than the buffer tail, append
        3. Trim to lookback window, persist timestamp, rebuild series
        4. Return updated CandleSeries or None if no new data
        """
        candle = await self.fetch_latest_candle(symbol)
        if candle is None:
            return None

        buf = self._buffers.get(symbol, [])
        if buf and candle.timestamp <= buf[-1].timestamp:
            return None  # No new candle yet

        updated = [*buf, candle][-self._lookback :]
        self._buffers[symbol] = updated
        series = self._build_series(symbol, updated)
        self._series[symbol] = series

        await self._state.set_state(
            f"last_candle_{symbol}", candle.timestamp.isoformat()
        )
        self._log.debug(
            "candle_updated",
            symbol=symbol,
            ts=candle.timestamp.isoformat(),
        )
        return series

    def get_candle_series(self, symbol: str) -> Optional[CandleSeries]:
        """Get current CandleSeries for a symbol. None if not initialized."""
        return self._series.get(symbol)

    async def wait_for_candle(
        self, symbol: str, timeout: float = 310,
        shutdown_event: Optional[asyncio.Event] = None,
    ) -> Optional[CandleSeries]:
        """Poll every 10s until a new candle arrives or timeout.

        This is the main entry point for the trading loop — it blocks
        until fresh candle data is available.
        """
        poll_interval = 10
        elapsed = 0.0

        while elapsed < timeout:
            if shutdown_event and shutdown_event.is_set():
                return None
            series = await self.update_candles(symbol)
            if series is not None:
                return series
            try:
                await asyncio.wait_for(
                    shutdown_event.wait() if shutdown_event else asyncio.sleep(poll_interval),
                    timeout=poll_interval,
                )
                # Event was set → shutting down
                return None
            except asyncio.TimeoutError:
                pass
            elapsed += poll_interval

        self._log.warning("wait_timeout", symbol=symbol, timeout=timeout)
        return None

    # ── helpers ────────────────────────────────────────────────

    @staticmethod
    def _build_series(symbol: str, candles: list[Candle]) -> CandleSeries:
        """Construct a CandleSeries from a candle list."""
        return CandleSeries(
            candles=candles,
            symbol=symbol,
            exchange="bybit",
            timeframe="5m",
        )
