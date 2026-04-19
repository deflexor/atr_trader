"""Momentum trading strategy.

Actual momentum: buys when price is trending UP with strength,
sells when price is trending DOWN with strength.

Uses:
- EMA crossover for trend direction
- RSI for trend strength (not overbought/oversold)
- MACD histogram for momentum acceleration
- Volume confirmation
- H1Model LSTM as confirmation filter (1h trend must agree)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import statistics

from .base_strategy import BaseStrategy, StrategyConfig
from ..core.models.signal import Signal, SignalDirection
from ..core.models.candle import Candle, CandleSeries
from ..core.models.market_data import MarketData
from ..ml.h1_model import H1Model


# Preset profiles keyed by quote-agnostic base (e.g. "BTC", "ETH", "DOGE", "TRX")
# Values override MomentumConfig defaults for that asset class.
#
# Design: relaxed from previous over-filtered profiles that produced zero trades.
# Key changes vs v1:
#   - min_agreement lowered to 2 for more frequent entries (was 3)
#   - pullback_enabled ON (0.3% retrace — relaxed for more signals)
#   - volume_spike_threshold reduced to 1.5x (was 2.0x — too strict, blocking 92%)
ASSET_PROFILES: dict[str, dict] = {
    # Default profiles: relaxed for higher signal capture
    "BTC": {
        "atr_filter_min_pct": 0.0005,  # Relaxed from 0.001 (0.05% vs 0.1%)
        "min_agreement": 2,
        "momentum_threshold": 0.005,
        "pullback_enabled": True,
        "pullback_retrace_min": 0.003,  # Relaxed from 0.005 (0.3% vs 0.5%)
        "rsi_divergence_enabled": False,
        "entry_candle_required": False,
        "mtf_enabled": False,
        "volume_spike_threshold": 1.5,  # Relaxed from 2.0 (was blocking 92% of signals)
    },
    "ETH": {
        "atr_filter_min_pct": 0.0005,
        "min_agreement": 2,
        "momentum_threshold": 0.005,
        "pullback_enabled": True,
        "pullback_retrace_min": 0.003,
        "rsi_divergence_enabled": False,
        "entry_candle_required": False,
        "mtf_enabled": False,
        "volume_spike_threshold": 1.5,
    },
    "DOGE": {
        "atr_filter_min_pct": 0.001,  # Slightly higher for volatile asset
        "min_agreement": 2,
        "momentum_threshold": 0.005,
        "pullback_enabled": True,
        "pullback_retrace_min": 0.004,  # 0.4% for DOGE
        "rsi_divergence_enabled": False,
        "entry_candle_required": False,
        "mtf_enabled": False,
        "volume_spike_threshold": 1.5,
    },
    "TRX": {
        "atr_filter_min_pct": 0.0005,
        "min_agreement": 2,
        "momentum_threshold": 0.005,
        "pullback_enabled": True,
        "pullback_retrace_min": 0.003,
        "rsi_divergence_enabled": False,
        "entry_candle_required": False,
        "mtf_enabled": False,
        "volume_spike_threshold": 1.5,
    },
    # Conservative profiles: tighter pullback, more agreement, entry candle confirmation
    "BTC_CONSERVATIVE": {
        "atr_filter_min_pct": 0.0005,
        "min_agreement": 4,
        "momentum_threshold": 0.006,
        "pullback_enabled": True,
        "pullback_retrace_min": 0.005,
        "rsi_divergence_enabled": True,
        "entry_candle_required": True,
        "mtf_enabled": True,
        "volume_spike_threshold": 2.0,  # Conservative: require stronger volume
    },
    "ETH_CONSERVATIVE": {
        "atr_filter_min_pct": 0.001,
        "min_agreement": 4,
        "momentum_threshold": 0.006,
        "pullback_enabled": True,
        "pullback_retrace_min": 0.005,
        "rsi_divergence_enabled": True,
        "entry_candle_required": True,
        "mtf_enabled": True,
        "volume_spike_threshold": 2.0,
    },
    # Aggressive profiles: no pullback, trade the breakout
    "BTC_AGGRESSIVE": {
        "atr_filter_min_pct": 0.0003,
        "min_agreement": 1,
        "momentum_threshold": 0.003,
        "pullback_enabled": False,
        "rsi_divergence_enabled": False,
        "entry_candle_required": False,
        "mtf_enabled": False,
        "volume_spike_threshold": 1.0,  # Aggressive: no volume filter
    },
    "ETH_AGGRESSIVE": {
        "atr_filter_min_pct": 0.0005,
        "min_agreement": 1,
        "momentum_threshold": 0.003,
        "pullback_enabled": False,
        "rsi_divergence_enabled": False,
        "entry_candle_required": False,
        "mtf_enabled": False,
        "volume_spike_threshold": 1.0,
    },
}


def _asset_base(symbol: str) -> str:
    """Extract base asset from symbol (BTCUSDT -> BTC, DOGE-USDT -> DOGE)."""
    base = symbol.upper().replace("-", "").replace("USDT", "").replace("BUSD", "")
    return base


@dataclass
class MomentumConfig(StrategyConfig):
    """Configuration for momentum strategy."""

    # Kelly Criterion sizing
    kelly_sizing_enabled: bool = False  # Use Kelly formula for position sizing
    kelly_max_pct: float = 0.10  # 10% cap on Kelly position size

    # Best trailing stop from optimization
    trailing_activation_atr: float = 3.0  # Activate when price moves 3.0*ATR
    trailing_distance_atr: float = 2.5  # Trail at 2.5*ATR behind extreme

    # Standard momentum settings
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_bullish_min: float = 50.0  # RSI above 50 = bullish momentum
    rsi_bearish_max: float = 50.0  # RSI below 50 = bearish momentum
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    momentum_threshold: float = 0.005  # 0.5% move to confirm momentum
    min_agreement: int = 2  # Minimum indicators that must agree (of 5)

    # Pullback / patience settings
    pullback_enabled: bool = True  # Wait for pullback within trend
    pullback_lookback: int = 10  # Look back N candles for the recent extreme
    pullback_retrace_min: float = 0.001  # Must have retraced at least 0.1% from extreme
    rsi_divergence_enabled: bool = (
        False  # Skip when RSI diverges from price (disabled: too restrictive)
    )
    rsi_divergence_lookback: int = 5  # Candles to check for divergence
    entry_candle_required: bool = (
        False  # Require bullish/bearish entry candle (OFF: too restrictive)
    )

    # Multi-timeframe confirmation
    mtf_enabled: bool = (
        False  # Filter entries against higher-timeframe trend (OFF: too restrictive)
    )
    mtf_timeframe: str = "1h"  # Higher timeframe to use
    mtf_ema_fast: int = 3  # Fast EMA on higher timeframe (responsive)
    mtf_ema_slow: int = 8  # Slow EMA on higher timeframe (responsive)

    # Volatility filter
    atr_filter_enabled: bool = True  # Skip entries in low-vol conditions
    atr_filter_min_pct: float = 0.001  # Min ATR as % of price (0.1%) to enter (was 0.2%)

    # Volume filter (relaxed from 2.0x to 1.5x to allow more signals)
    volume_spike_threshold: float = 1.5  # Volume must be 1.5x average to enter (was 2.0x)

    # Regime selection: overrides asset-base lookup when set
    # e.g. "BTC_AGGRESSIVE", "BTC_CONSERVATIVE", "ETH_AGGRESSIVE"
    regime: str = ""  # Empty = auto-detect from symbol

    # Auto-regime detection (volatility-based)
    regime_detection: bool = False  # Enable ATR-percentile-based regime switching
    regime_atr_lookback: int = 100  # Lookback for ATR percentile
    regime_high_percentile: float = 0.80  # Above this → conservative
    regime_low_percentile: float = 0.20  # Below this → aggressive


class MomentumStrategy(BaseStrategy):
    """Trend-following momentum strategy.

    Genuine momentum: aligns multiple trend indicators.
    Goes LONG when: EMA fast > slow AND RSI > 50 AND MACD histogram positive
    Goes SHORT when: EMA fast < slow AND RSI < 50 AND MACD histogram negative

    H1Model Integration:
        - 1m signal generated from price action
        - H1Model checks 1h LSTM confirmation
        - If 1h trend agrees, proceed; otherwise filter out
    """

    def __init__(self, config: Optional[MomentumConfig] = None, h1_model: Optional[H1Model] = None):
        super().__init__(config or MomentumConfig(name="momentum"))
        self.momentum_config = config or MomentumConfig(name="momentum")
        self.h1_model = h1_model  # Optional: for 1h trend confirmation
        self._asset_overrides: dict[str, MomentumConfig] = {}
        self._mtf_hour_bucket: int = -1
        self._mtf_resampled: list[list[float]] = []  # [[o, h, l, c, v], ...]
        self._mtf_symbol: str = ""
        self._mtf_exchange: str = ""
        # Kelly sizing tracker
        self._kelly_sizer = KellyPositionSizer(max_kelly_pct=self.momentum_config.kelly_max_pct)
        # Diagnostics: count signals at each filter stage
        self.diagnostics: dict[str, int] = {
            "total_evaluated": 0,
            "atr_filtered": 0,
            "indicators_computed": 0,
            "min_agreement_passed": 0,
            "mtf_filtered": 0,
            "h1_model_filtered": 0,
            "pullback_filtered": 0,
            "rsi_divergence_filtered": 0,
            "entry_candle_filtered": 0,
            "signals_produced": 0,
        }

    def _config_for_symbol(self, symbol: str) -> MomentumConfig:
        """Return MomentumConfig with asset-specific overrides applied."""
        # If regime is explicitly set (e.g. "BTC_AGGRESSIVE"), use it directly
        if self.momentum_config.regime:
            base = self.momentum_config.regime
        else:
            base = _asset_base(symbol)

        if base in self._asset_overrides:
            return self._asset_overrides[base]

        profile = ASSET_PROFILES.get(base)
        if profile is None:
            return self.momentum_config

        import dataclasses

        overrides = dataclasses.replace(self.momentum_config, **profile)
        self._asset_overrides[base] = overrides
        return overrides

    def calculate_ema(self, prices: list[float], period: int) -> Optional[float]:
        """Calculate EMA value."""
        if len(prices) < period:
            return None
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def calculate_rsi(self, prices: list[float], period: int = 14) -> Optional[float]:
        """Calculate RSI indicator."""
        if len(prices) < period + 1:
            return None

        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]

        avg_gain = statistics.mean(gains) if gains else 0
        avg_loss = statistics.mean(losses) if losses else 0

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def calculate_macd(
        self,
        prices: list[float],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Optional[tuple[float, float, float]]:
        """Calculate MACD (macd_line, signal_line, histogram)."""
        if len(prices) < slow + signal:
            return None

        def ema_series(data: list[float], period: int) -> list[float]:
            if not data:
                return []
            multiplier = 2 / (period + 1)
            result = [data[0]]
            for price in data[1:]:
                result.append((price - result[-1]) * multiplier + result[-1])
            return result

        ema_fast = ema_series(prices, fast)
        ema_slow = ema_series(prices, slow)

        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(ema_slow))]
        signal_line = ema_series(macd_line, signal)

        if len(macd_line) < signal or len(signal_line) < 1:
            return None

        histogram = macd_line[-1] - signal_line[-1]
        return macd_line[-1], signal_line[-1], histogram

    def calculate_momentum(self, prices: list[float], period: int = 10) -> float:
        """Calculate price momentum as rate of change."""
        if len(prices) < period + 1:
            return 0.0
        return (prices[-1] - prices[-period]) / prices[-period]

    def _volume_trend(self, candles: CandleSeries, lookback: int = 20) -> float:
        """Check if volume is increasing in trend direction.

        Returns positive if volume supports upward moves, negative for downward.
        """
        if len(candles.candles) < lookback:
            return 0.0

        recent = candles.candles[-lookback:]
        up_volume = sum(c.volume for c in recent if c.close > c.open)
        down_volume = sum(c.volume for c in recent if c.close <= c.open)
        total = up_volume + down_volume

        if total == 0:
            return 0.0
        return (up_volume - down_volume) / total

    def _check_volume_spike(self, candles: CandleSeries, mult: float = 2.0) -> bool:
        """Check if current volume is significantly above average.

        Used to filter entries during low-volume choppy markets.
        Returns True if volume spike detected (don't block).
        """
        lookback = 20
        if len(candles.candles) < lookback + 1:
            return True  # Not enough data, don't block

        recent_volumes = [c.volume for c in candles.candles[-lookback:]]
        avg_volume = sum(recent_volumes) / len(recent_volumes)
        current_volume = candles.candles[-1].volume

        return current_volume >= avg_volume * mult

    def _check_pullback(
        self,
        closes: list[float],
        ema_fast: float,
        is_long: bool,
        lookback: int,
        retrace_min: float,
    ) -> bool:
        """Check if price has pulled back within a confirmed trend.

        Verifies:
        1. A recent extreme (high for longs, low for shorts) existed above/below current price
        2. Current price has retraced toward the EMA by at least retrace_min

        The near-EMA check is intentionally removed — conflating pullback detection
        (price dipped from extreme) with entry confirmation (price near fair value)
        caused over-filtering. We only check that price has actually pulled back.
        """
        if len(closes) < lookback + 1:
            return True  # Not enough data, don't block

        recent = closes[-lookback:]
        current_price = closes[-1]

        if is_long:
            recent_high = max(recent)
            retraced = (recent_high - current_price) / recent_high
            return retraced >= retrace_min
        else:
            recent_low = min(recent)
            retraced = (current_price - recent_low) / recent_low
            return retraced >= retrace_min

    def _check_rsi_divergence(
        self,
        closes: list[float],
        rsi: float,
        lookback: int,
        is_long: bool,
    ) -> bool:
        """Detect RSI divergence: momentum weakening against price direction.

        Returns True if divergence is detected (should SKIP entry).
        For longs: price rising but RSI falling = bearish divergence.
        For shorts: price falling but RSI rising = bullish divergence.
        """
        if len(closes) < lookback + 1:
            return False  # Not enough data

        prev_rsi = self.calculate_rsi(closes[:-1], self.momentum_config.rsi_period)
        if prev_rsi is None:
            return False

        price_change = closes[-1] - closes[-lookback]

        if is_long:
            # Bearish divergence: price rising but RSI dropping
            if price_change > 0 and rsi < prev_rsi:
                return True
        else:
            # Bullish divergence: price falling but RSI rising
            if price_change < 0 and rsi > prev_rsi:
                return True

        return False

    def _check_entry_candle(self, candles: CandleSeries, is_long: bool) -> bool:
        """Validate the current entry candle direction matches trade direction.

        For longs: current candle must be bullish (close > open).
        For shorts: current candle must be bearish (close < open).
        """
        if not candles.candles:
            return False

        current = candles.candles[-1]
        if is_long:
            return current.close > current.open
        else:
            return current.close < current.open

    def _previous_candle_level(self, candles: CandleSeries, is_long: bool) -> Optional[float]:
        """Get previous candle low (for longs) or high (for shorts) as entry threshold.

        Returns None if not enough candles available.
        """
        if len(candles.candles) < 2:
            return None

        prev = candles.candles[-2]
        return prev.low if is_long else prev.high

    def _calculate_atr(self, candles: CandleSeries, period: int = 14) -> float:
        """Calculate Average True Range for the candle series.

        Uses the same ATR formula as the backtest engine.
        """
        if len(candles.candles) < period + 1:
            return 0.0

        true_ranges = []
        for i in range(-period, 0):
            c = candles.candles[i]
            prev_c = candles.candles[i - 1]
            tr = max(
                c.high - c.low,
                abs(c.high - prev_c.close),
                abs(c.low - prev_c.close),
            )
            true_ranges.append(tr)

        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def calculate_adx(self, candles: CandleSeries, period: int = 14) -> float:
        """Calculate Average Directional Index (ADX) to measure trend strength.

        ADX > 25 indicates TRENDING market
        ADX <= 25 indicates RANGING market

        Uses the standard ADX calculation:
        - +DM (directional movement positive)
        - -DM (directional movement negative)
        - True Range
        - Smoothed averages

        Returns:
            ADX value (0-100)
        """
        if len(candles.candles) < period * 2 + 1:
            return 0.0

        n = len(candles.candles)

        # Calculate True Range and Directional Movements
        tr_list = []
        plus_dm_list = []
        minus_dm_list = []

        for i in range(1, n):
            c = candles.candles[i]
            prev_c = candles.candles[i - 1]

            # True Range
            tr = max(
                c.high - c.low,
                abs(c.high - prev_c.close),
                abs(c.low - prev_c.close),
            )
            tr_list.append(tr)

            # +DM and -DM
            high_diff = c.high - prev_c.high
            low_diff = prev_c.low - c.low

            plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
            minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0.0

            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        if len(tr_list) < period:
            return 0.0

        # Smooth using Wilder's method (similar to ATR calculation)
        # Initial smoothed values
        smoothed_tr = sum(tr_list[:period])
        smoothed_plus_dm = sum(plus_dm_list[:period])
        smoothed_minus_dm = sum(minus_dm_list[:period])

        # Calculate smoothed +DI and -DI
        plus_di_list = []
        minus_di_list = []
        dx_list = []

        for i in range(period, len(tr_list)):
            # Update smoothed values
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
            smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_list[i]
            smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_list[i]

            if smoothed_tr == 0:
                continue

            plus_di = 100 * (smoothed_plus_dm / smoothed_tr)
            minus_di = 100 * (smoothed_minus_dm / smoothed_tr)

            di_sum = plus_di + minus_di
            if di_sum == 0:
                dx = 0
            else:
                dx = 100 * abs(plus_di - minus_di) / di_sum

            plus_di_list.append(plus_di)
            minus_di_list.append(minus_di)
            dx_list.append(dx)

        if len(dx_list) < period:
            return 0.0

        # Calculate ADX as the average of DX values
        adx = sum(dx_list[-period:]) / period
        return adx

    def detect_market_regime(self, candles: CandleSeries) -> str:
        """Detect if market is TRENDING or RANGING using ADX.

        ADX > 25 = TRENDING (strong directional movement)
        ADX <= 25 = RANGING (weak/no directional movement)

        Args:
            candles: Candle series for analysis

        Returns:
            "TRENDING" or "RANGING"
        """
        adx = self.calculate_adx(candles, period=14)
        if adx > 25:
            return "TRENDING"
        return "RANGING"

    def detect_regime(self, candles: CandleSeries) -> str:
        """Detect market regime based on ATR percentile.

        High volatility (ATR > high_percentile of history) → CONSERVATIVE
        Low volatility (ATR < low_percentile of history) → AGGRESSIVE
        Middle → DEFAULT (use standard profile)

        Returns "AGGRESSIVE", "CONSERVATIVE", or "" (use default).
        """
        high_p = self.momentum_config.regime_high_percentile
        low_p = self.momentum_config.regime_low_percentile
        min_data = 50  # Minimum candles needed

        n = len(candles.candles)
        if n < min_data + 14:
            return ""  # Insufficient data

        # Compute ATR % series over ALL available history (not just a fixed window)
        atr_pcts = []
        for i in range(14, n):
            atr = self._calculate_atr(
                CandleSeries(
                    candles=candles.candles[i - 14 : i + 1],
                    symbol=candles.symbol,
                    exchange=candles.exchange,
                    timeframe=candles.timeframe,
                ),
                period=14,
            )
            price = candles.candles[i].close
            if price > 0:
                atr_pcts.append(atr / price)

        if len(atr_pcts) < 20:
            return ""

        current_atr_pct = atr_pcts[-1]

        # Percentile: what fraction of history was below current
        below_count = sum(1 for v in atr_pcts[:-1] if v < current_atr_pct)
        percentile = below_count / (len(atr_pcts) - 1)

        if percentile > high_p:
            return "CONSERVATIVE"
        elif percentile < low_p:
            return "AGGRESSIVE"
        else:
            return ""  # Middle → default profile

    def _check_volatility_filter(
        self,
        candles: CandleSeries,
        atr_threshold_pct: float,
    ) -> bool:
        """Check if market volatility is sufficient for momentum entries.

        Compares ATR as a percentage of current price against the threshold.
        Works identically across assets regardless of price level.

        Returns True if volatility is sufficient (don't block entry).
        """
        if atr_threshold_pct <= 0:
            return True

        atr = self._calculate_atr(candles, period=14)
        if not candles.candles or atr <= 0:
            return False

        current_price = candles.candles[-1].close
        atr_pct = atr / current_price
        return atr_pct >= atr_threshold_pct

    def _check_mtf_trend(
        self,
        candles: CandleSeries,
        is_long: bool,
        timeframe: str,
        ema_fast_period: int,
        ema_slow_period: int,
    ) -> bool:
        """Check if higher-timeframe trend aligns with entry direction.

        Incrementally builds 1h candles from 5m data to avoid O(n²) resampling.
        Only appends a new 1h bar when the candle count crosses into a new hour bucket.

        Returns True if MTF trend confirms the entry direction.
        """
        n = len(candles.candles)
        current_bucket = n // 12

        # Incrementally add new 1h bars as they complete
        while len(self._mtf_resampled) < current_bucket:
            start_idx = len(self._mtf_resampled) * 12
            end_idx = min(start_idx + 12, n)
            if end_idx - start_idx < 1:
                break

            group = candles.candles[start_idx:end_idx]
            self._mtf_resampled.append(
                [
                    group[0].open,
                    max(c.high for c in group),
                    min(c.low for c in group),
                    group[-1].close,
                    sum(c.volume for c in group),
                ]
            )

        # Add the current incomplete hour bar
        partial_start = current_bucket * 12
        closes_1h = [bar[3] for bar in self._mtf_resampled]
        if partial_start < n:
            partial = candles.candles[partial_start:n]
            if partial:
                closes_1h.append(partial[-1].close)

        if len(closes_1h) < ema_slow_period:
            return True  # Not enough higher-TF data

        ema_f = self.calculate_ema(closes_1h, ema_fast_period)
        ema_s = self.calculate_ema(closes_1h, ema_slow_period)

        if ema_f is None or ema_s is None:
            return True

        if is_long:
            return ema_f > ema_s
        else:
            return ema_f < ema_s

    def _get_1h_candles(self, candles_1m: CandleSeries) -> list:
        """Extract 1h candles from 1m series by aggregation.

        Args:
            candles_1m: 1-minute candle series

        Returns:
            List of 1h Candle objects suitable for H1Model
        """
        if len(candles_1m.candles) < 60:
            return []

        # Aggregate 1m candles into 1h buckets
        h1_candles = []
        n = len(candles_1m.candles)

        for i in range(0, n, 60):
            chunk = candles_1m.candles[i : min(i + 60, n)]
            if len(chunk) < 30:  # Need at least 30 mins of data
                continue

            h1_candle = Candle(
                symbol=chunk[0].symbol,
                exchange=chunk[0].exchange,
                timeframe="1h",
                timestamp=chunk[0].timestamp,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
            )
            h1_candles.append(h1_candle)

        return h1_candles

    async def multi_timeframe_signal(
        self,
        symbol: str,
        candles_1m: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> tuple[Signal, dict]:
        """Combine 1m signal with 1h LSTM confirmation.

        Strategy:
            1. Get 1m signal from momentum indicators
            2. Get 1h LSTM prediction for trend confirmation
            3. Only proceed if both agree on direction

        Args:
            symbol: Trading pair
            candles_1m: 1-minute candles for entry signals
            market_data: Optional market data

        Returns:
            (signal, mtf_info) tuple where mtf_info contains H1Model data
        """
        mtf_info = {
            "1m_direction": None,
            "h1_direction": None,
            "h1_confidence": 0.0,
            "h1_trend_agrees": False,
            "h1_probabilities": None,
        }

        # Step 1: Generate 1m signal
        signal_1m = await self.generate_signal(symbol, candles_1m, market_data)
        mtf_info["1m_direction"] = signal_1m.direction.value if signal_1m.direction else "NEUTRAL"

        # If 1m signal is neutral, no need to check H1Model
        if signal_1m.direction == SignalDirection.NEUTRAL:
            return signal_1m, mtf_info

        # Step 2: Get H1Model confirmation if available
        if self.h1_model is None:
            mtf_info["h1_trend_agrees"] = True  # No H1Model, skip confirmation
            return signal_1m, mtf_info

        h1_candles = self._get_1h_candles(candles_1m)
        if not h1_candles:
            mtf_info["h1_trend_agrees"] = True  # Not enough 1h data, skip
            return signal_1m, mtf_info

        trend_agrees, h1_direction, h1_confidence, h1_probs = (
            self.h1_model.confirm_trend(h1_candles, market_data)
        )

        mtf_info["h1_direction"] = {0: "DOWN", 1: "FLAT", 2: "UP"}.get(h1_direction, "UNKNOWN")
        mtf_info["h1_confidence"] = h1_confidence
        mtf_info["h1_trend_agrees"] = trend_agrees
        mtf_info["h1_probabilities"] = h1_probs.tolist() if h1_probs is not None else None

        # If H1Model disagrees, neutralize the signal
        if not trend_agrees:
            signal_1m.direction = SignalDirection.NEUTRAL
            signal_1m.strength = 0.0
            signal_1m.confidence = 0.0

        return signal_1m, mtf_info

    async def generate_signal(
        self,
        symbol: str,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> Signal:
        """Generate momentum-based trading signal.

        Real momentum: multiple indicators agree on direction.
        Requires strong consensus before trading.
        """
        if len(candles.candles) < 50:
            return Signal(
                symbol=symbol,
                exchange=candles.exchange or "kucoin",
                direction=SignalDirection.NEUTRAL,
                strength=0.0,
                confidence=0.0,
                strategy_id=self.config.name,
            )

        # Track diagnostics
        self.diagnostics["total_evaluated"] += 1

        # Auto-regime detection: set regime before config resolution if enabled
        if self.momentum_config.regime_detection and not self.momentum_config.regime:
            detected = self.detect_regime(candles)
            if detected:
                base = _asset_base(symbol)
                self.momentum_config.regime = f"{base}_{detected}"

        # Resolve per-asset config
        cfg = self._config_for_symbol(symbol)

        # --- Volatility filter: skip choppy markets ---
        if cfg.atr_filter_enabled:
            if not self._check_volatility_filter(candles, cfg.atr_filter_min_pct):
                self.diagnostics["atr_filtered"] += 1
                return Signal(
                    symbol=symbol,
                    exchange=candles.exchange or "kucoin",
                    direction=SignalDirection.NEUTRAL,
                    strength=0.0,
                    confidence=0.0,
                    strategy_id=self.config.name,
                )

        closes = candles.closes

        # Calculate indicators
        ema_fast = self.calculate_ema(closes, cfg.ema_fast)
        ema_slow = self.calculate_ema(closes, cfg.ema_slow)
        rsi = self.calculate_rsi(closes, cfg.rsi_period)
        macd = self.calculate_macd(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
        momentum = self.calculate_momentum(closes, 10)
        vol_trend = self._volume_trend(candles)

        # Score each indicator: +1 for bullish, -1 for bearish
        bull_votes = 0
        bear_votes = 0
        total_indicators = 0

        # EMA trend: fast above slow = bullish
        if ema_fast is not None and ema_slow is not None:
            total_indicators += 1
            if ema_fast > ema_slow:
                bull_votes += 1
            elif ema_fast < ema_slow:
                bear_votes += 1

        # RSI: above 50 = bullish momentum (NOT overbought/oversold)
        if rsi is not None:
            total_indicators += 1
            if rsi > cfg.rsi_bullish_min:
                bull_votes += 1
            elif rsi < cfg.rsi_bearish_max:
                bear_votes += 1

        # MACD: histogram positive and rising = bullish
        if macd is not None:
            total_indicators += 1
            macd_line, signal_line, histogram = macd
            if histogram > 0 and macd_line > signal_line:
                bull_votes += 1
            elif histogram < 0 and macd_line < signal_line:
                bear_votes += 1

        # Price momentum: positive ROC = bullish
        if abs(momentum) > cfg.momentum_threshold:
            total_indicators += 1
            if momentum > 0:
                bull_votes += 1
            else:
                bear_votes += 1

        # Volume confirmation
        if abs(vol_trend) > 0.1:
            total_indicators += 1
            if vol_trend > 0:
                bull_votes += 1
            else:
                bear_votes += 1

        # Determine direction: require minimum agreement
        direction = SignalDirection.NEUTRAL
        strength = 0.0
        confidence = 0.0
        is_long = False

        if bull_votes >= cfg.min_agreement and bull_votes > bear_votes:
            direction = SignalDirection.LONG
            strength = min(1.0, bull_votes / max(total_indicators, 1))
            confidence = bull_votes / max(total_indicators, 1)
            is_long = True
        elif bear_votes >= cfg.min_agreement and bear_votes > bull_votes:
            direction = SignalDirection.SHORT
            strength = min(1.0, bear_votes / max(total_indicators, 1))
            confidence = bear_votes / max(total_indicators, 1)

        self.diagnostics["indicators_computed"] += 1

        # --- Volume & patience filters ---
        if direction != SignalDirection.NEUTRAL:
            self.diagnostics["min_agreement_passed"] += 1

            # 0. Volume spike: reject entries in low-volume chop (use configured threshold)
            if not self._check_volume_spike(candles, mult=cfg.volume_spike_threshold):
                direction = SignalDirection.NEUTRAL
                strength = 0.0
                confidence = 0.0
                self.diagnostics["volume_filtered"] = self.diagnostics.get("volume_filtered", 0) + 1

            # 1. Multi-timeframe: 1h trend must confirm direction
            if cfg.mtf_enabled:
                if not self._check_mtf_trend(
                    candles, is_long, cfg.mtf_timeframe, cfg.mtf_ema_fast, cfg.mtf_ema_slow
                ):
                    direction = SignalDirection.NEUTRAL
                    strength = 0.0
                    confidence = 0.0
                    self.diagnostics["mtf_filtered"] += 1

            # 2. H1Model LSTM confirmation: 1h trend must agree with 1m direction
            if direction != SignalDirection.NEUTRAL and self.h1_model is not None:
                # Get 1h candles for H1Model confirmation
                h1_candles = self._get_1h_candles(candles)
                if h1_candles and len(h1_candles) >= 60:
                    _, h1_direction, h1_confidence, h1_probs = (
                        self.h1_model.confirm_trend(h1_candles, market_data)
                    )
                    # Block if 1h LSTM disagrees with 1m signal direction
                    # H1Model: 2=UP, 1=FLAT, 0=DOWN
                    # is_long=True means 1m signal is LONG (want H1Model UP)
                    # is_long=False means 1m signal is SHORT (want H1Model DOWN)
                    h1_agrees = (
                        (is_long and h1_direction == 2) or
                        (not is_long and h1_direction == 0)
                    )
                    if not h1_agrees:
                        direction = SignalDirection.NEUTRAL
                        strength = 0.0
                        confidence = 0.0
                        self.diagnostics["h1_model_filtered"] += 1

            # 3. Pullback: wait for price to dip from recent extreme
            if cfg.pullback_enabled and ema_fast is not None:
                if not self._check_pullback(
                    closes, ema_fast, is_long, cfg.pullback_lookback, cfg.pullback_retrace_min
                ):
                    direction = SignalDirection.NEUTRAL
                    strength = 0.0
                    confidence = 0.0
                    self.diagnostics["pullback_filtered"] += 1

            # 2. RSI divergence: skip if momentum weakening
            if (
                direction != SignalDirection.NEUTRAL
                and cfg.rsi_divergence_enabled
                and rsi is not None
            ):
                if self._check_rsi_divergence(closes, rsi, cfg.rsi_divergence_lookback, is_long):
                    direction = SignalDirection.NEUTRAL
                    strength = 0.0
                    confidence = 0.0
                    self.diagnostics["rsi_divergence_filtered"] += 1

            # 3. Entry candle: must match trade direction
            if direction != SignalDirection.NEUTRAL and cfg.entry_candle_required:
                if not self._check_entry_candle(candles, is_long):
                    direction = SignalDirection.NEUTRAL
                    strength = 0.0
                    confidence = 0.0
                    self.diagnostics["entry_candle_filtered"] += 1

        current_price = closes[-1] if closes else 0.0

        signal = Signal(
            symbol=symbol,
            exchange=candles.exchange or "kucoin",
            direction=direction,
            strength=strength,
            confidence=confidence,
            price=current_price,
            strategy_id=self.config.name,
        )

        # Attach entry level from previous candle for execution layer
        if direction != SignalDirection.NEUTRAL:
            entry_level = self._previous_candle_level(candles, is_long)
            if entry_level is not None:
                signal.features = {"entry_level": entry_level}
            self.diagnostics["signals_produced"] += 1

        return signal

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        risk_per_trade: float = 0.02,
    ) -> float:
        """Calculate position size based on risk management or Kelly Criterion.

        Args:
            signal: Trading signal with direction and price
            portfolio_value: Current portfolio value
            risk_per_trade: Default risk % if Kelly disabled

        Returns:
            Position size in base currency units
        """
        if signal.direction == SignalDirection.NEUTRAL:
            return 0.0

        if self.momentum_config.kelly_sizing_enabled:
            kelly_pct = self._kelly_sizer.calculate_kelly_pct()
            effective_risk = kelly_pct * signal.strength if signal.strength > 0 else kelly_pct
            position_value = portfolio_value * effective_risk
            if signal.price > 0:
                return position_value / signal.price
            return 0.0

        risk_amount = portfolio_value * risk_per_trade
        adjusted_risk = risk_amount * signal.strength

        if signal.price > 0:
            return adjusted_risk / signal.price

        return 0.0

    def record_trade_for_kelly(self, pnl: float) -> None:
        """Record a closed trade's PnL for Kelly calculation.

        Call this after each trade closes to update Kelly statistics.
        """
        self._kelly_sizer.record_trade(pnl)


def calculate_kelly_fraction(win_rate: float, avg_win_loss_ratio: float) -> float:
    """Calculate Kelly Criterion position size as fraction of capital.

    Formula: f* = (b × p - q) / b
    Where: p = win rate, q = 1-p, b = odds ratio (avg_win / avg_loss)

    Args:
        win_rate: Probability of winning (0.0 to 1.0)
        avg_win_loss_ratio: Ratio of average win to average loss (e.g., 2.0 = twice as big as loss)

    Returns:
        Kelly percentage capped at 10% for safety
    """
    if avg_win_loss_ratio <= 0 or win_rate < 0 or win_rate > 1:
        return 0.0

    p = win_rate
    q = 1.0 - p
    b = avg_win_loss_ratio

    # Kelly formula: (b * p - q) / b
    kelly = (b * p - q) / b

    # Cap at 10% maximum (practical safety limit)
    return max(0.0, min(0.10, kelly))


class KellyPositionSizer:
    """Tracks trade history and calculates Kelly-based position sizes.

    Pure implementation - no side effects, thread-safe.
    """

    def __init__(self, max_kelly_pct: float = 0.10):
        self.max_kelly_pct = max_kelly_pct
        self._pnl_history: list[float] = []

    def record_trade(self, pnl: float) -> None:
        """Record a closed trade's PnL for Kelly calculation."""
        self._pnl_history.append(pnl)

    def reset(self) -> None:
        """Clear trade history."""
        self._pnl_history = []

    @property
    def trade_count(self) -> int:
        return len(self._pnl_history)

    def calculate_kelly_pct(self) -> float:
        """Calculate Kelly percentage from trade history.

        Requires at least 10 trades for meaningful statistics.

        Returns:
            Kelly fraction (0.0 to max_kelly_pct)
        """
        if len(self._pnl_history) < 10:
            return self.max_kelly_pct  # Use conservative default until enough data

        wins = [p for p in self._pnl_history if p > 0]
        losses = [p for p in self._pnl_history if p <= 0]

        if not wins or not losses:
            return self.max_kelly_pct

        win_rate = len(wins) / len(self._pnl_history)
        avg_win = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))

        if avg_loss == 0:
            return self.max_kelly_pct

        odds_ratio = avg_win / avg_loss
        kelly = calculate_kelly_fraction(win_rate, odds_ratio)

        return max(0.0, min(self.max_kelly_pct, kelly))

    def get_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        fallback_risk_pct: float = 0.015,
    ) -> float:
        """Calculate position size using Kelly formula.

        Args:
            signal: Trading signal with direction and price
            portfolio_value: Current portfolio value
            fallback_risk_pct: Default risk % if insufficient trade history

        Returns:
            Position size in base currency units
        """
        if signal.direction == SignalDirection.NEUTRAL or signal.price <= 0:
            return 0.0

        kelly_pct = self.calculate_kelly_pct()
        position_value = portfolio_value * kelly_pct

        return position_value / signal.price
