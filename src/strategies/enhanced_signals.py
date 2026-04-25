"""Enhanced signal generation — breakout, mean-reversion, trend, VWAP, and divergence signals.

Pure functions that produce trading signals from candle data.
Union logic: any sub-signal fires → trade, with signal_source
tracked in metadata for diagnostics and sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import statistics

from ..core.models.signal import Signal, SignalDirection
from ..core.models.candle import CandleSeries


@dataclass(frozen=True)
class EnhancedSignalConfig:
    """Configuration for enhanced signal generation."""

    # Breakout signal params
    breakout_lookback: int = 20  # N-candle high/low window
    breakout_volume_mult: float = 1.0  # Volume multiplier (1.0 = no volume filter)
    breakout_min_range_pct: float = 0.0  # Min breakout candle range as % of price (0 = off)

    # Mean-reversion signal params
    rsi_period: int = 14
    rsi_oversold: float = 35.0  # Slightly relaxed from 30 for more signals
    rsi_overbought: float = 65.0  # Slightly relaxed from 70 for more signals
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    bollinger_required: bool = False  # Require Bollinger touch (False = RSI alone sufficient)

    # Trend signal params (mirrors MomentumConfig)
    ema_fast: int = 9
    ema_slow: int = 21
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    min_agreement: int = 3  # All 3 indicators must agree for trend signal

    # Strength defaults per signal type
    breakout_strength: float = 0.6
    mean_reversion_strength: float = 0.5
    trend_strength: float = 0.7

    # VWAP deviation signal params
    vwap_period: int = 100  # candles for VWAP calculation
    vwap_deviation_threshold: float = 0.02  # 2% deviation from VWAP triggers
    vwap_enabled: bool = True

    # Momentum divergence signal params
    divergence_lookback: int = 20  # candles to check for divergence
    divergence_rsi_threshold: float = 10.0  # RSI must differ by this much
    divergence_enabled: bool = True


@dataclass(frozen=True)
class SubSignal:
    """A single sub-signal from one signal type."""

    direction: SignalDirection
    strength: float
    source: str  # "breakout", "mean_reversion", "trend", "vwap", "divergence"


def _calculate_ema(prices: list[float], period: int) -> Optional[float]:
    """Calculate EMA value from price list."""
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def _calculate_rsi(prices: list[float], period: int = 14) -> Optional[float]:
    """Calculate RSI from price list."""
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


def _calculate_bollinger(
    prices: list[float], period: int, num_std: float
) -> Optional[tuple[float, float, float]]:
    """Calculate Bollinger Bands (upper, middle, lower)."""
    if len(prices) < period:
        return None
    recent = prices[-period:]
    middle = statistics.mean(recent)
    std = statistics.stdev(recent) if len(recent) > 1 else 0.0
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def _calculate_macd(
    prices: list[float], fast: int, slow: int, signal: int
) -> Optional[tuple[float, float, float]]:
    """Calculate MACD (line, signal, histogram)."""
    if len(prices) < slow + signal:
        return None

    def ema_series(data: list[float], period: int) -> list[float]:
        if not data:
            return []
        mult = 2 / (period + 1)
        result = [data[0]]
        for p in data[1:]:
            result.append((p - result[-1]) * mult + result[-1])
        return result

    ema_f = ema_series(prices, fast)
    ema_s = ema_series(prices, slow)
    macd_line = [ema_f[i] - ema_s[i] for i in range(len(ema_s))]
    sig_line = ema_series(macd_line, signal)
    if len(macd_line) < signal or len(sig_line) < 1:
        return None
    histogram = macd_line[-1] - sig_line[-1]
    return macd_line[-1], sig_line[-1], histogram


def _avg_volume(candles: CandleSeries, lookback: int) -> float:
    """Average volume over lookback candles."""
    if len(candles.candles) < lookback:
        lookback = len(candles.candles)
    if lookback == 0:
        return 0.0
    recent = candles.candles[-lookback:]
    return sum(c.volume for c in recent) / len(recent)


def compute_breakout_signal(
    candles: CandleSeries, config: EnhancedSignalConfig
) -> SubSignal:
    """Detect N-candle high/low breakout with volume confirmation.

    LONG when close breaks above N-candle high.
    SHORT when close breaks below N-candle low.
    Requires volume > average * multiplier to confirm.
    """
    n = config.breakout_lookback
    if len(candles.candles) < n + 1:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "breakout")

    window = candles.candles[-(n + 1) : -1]  # previous N candles (exclude current)
    current = candles.candles[-1]
    window_high = max(c.high for c in window)
    window_low = min(c.low for c in window)

    # Volume check (optional — controlled by breakout_volume_mult)
    avg_vol = _avg_volume(candles, n)
    if avg_vol <= 0:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "breakout")
    vol_ok = config.breakout_volume_mult <= 1.0 or current.volume >= avg_vol * config.breakout_volume_mult

    # Range check — breakout candle must have meaningful range (0 = off)
    price = current.close
    range_pct = (current.high - current.low) / price if price > 0 else 0.0
    range_ok = config.breakout_min_range_pct <= 0.0 or range_pct >= config.breakout_min_range_pct

    if current.close > window_high and vol_ok and range_ok:
        return SubSignal(SignalDirection.LONG, config.breakout_strength, "breakout")
    if current.close < window_low and vol_ok and range_ok:
        return SubSignal(SignalDirection.SHORT, config.breakout_strength, "breakout")

    return SubSignal(SignalDirection.NEUTRAL, 0.0, "breakout")


def compute_mean_reversion_signal(
    candles: CandleSeries, config: EnhancedSignalConfig
) -> SubSignal:
    """Detect RSI oversold/overbought bounce with Bollinger Band touch.

    LONG when RSI < oversold AND price at/below lower Bollinger Band.
    SHORT when RSI > overbought AND price at/above upper Bollinger Band.
    """
    if len(candles.candles) < max(config.bollinger_period, config.rsi_period) + 1:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "mean_reversion")

    closes = candles.closes
    current_price = closes[-1]

    rsi = _calculate_rsi(closes, config.rsi_period)
    bands = _calculate_bollinger(closes, config.bollinger_period, config.bollinger_std)

    if rsi is None or bands is None:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "mean_reversion")

    upper, _middle, lower = bands

    # LONG: oversold + (Bollinger touch OR bollinger_required=False)
    if rsi < config.rsi_oversold:
        if not config.bollinger_required or current_price <= lower:
            return SubSignal(SignalDirection.LONG, config.mean_reversion_strength, "mean_reversion")

    # SHORT: overbought + (Bollinger touch OR bollinger_required=False)
    if rsi > config.rsi_overbought:
        if not config.bollinger_required or current_price >= upper:
            return SubSignal(SignalDirection.SHORT, config.mean_reversion_strength, "mean_reversion")

    return SubSignal(SignalDirection.NEUTRAL, 0.0, "mean_reversion")


def compute_trend_signal(
    candles: CandleSeries, config: EnhancedSignalConfig
) -> SubSignal:
    """Detect trend using EMA crossover + RSI + MACD voting.

    Same logic as MomentumStrategy but as a pure function.
    Requires min_agreement indicators to agree on direction.
    """
    min_candles = max(config.ema_slow + config.macd_signal, 50)
    if len(candles.candles) < min_candles:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "trend")

    closes = candles.closes

    # Calculate indicators
    ema_fast = _calculate_ema(closes, config.ema_fast)
    ema_slow = _calculate_ema(closes, config.ema_slow)
    rsi = _calculate_rsi(closes, config.rsi_period)
    macd = _calculate_macd(closes, config.macd_fast, config.macd_slow, config.macd_signal)

    bull_votes = 0
    bear_votes = 0
    total = 0

    # EMA trend
    if ema_fast is not None and ema_slow is not None:
        total += 1
        if ema_fast > ema_slow:
            bull_votes += 1
        elif ema_fast < ema_slow:
            bear_votes += 1

    # RSI momentum (above 50 = bullish, below 50 = bearish)
    if rsi is not None:
        total += 1
        if rsi > 50:
            bull_votes += 1
        elif rsi < 50:
            bear_votes += 1

    # MACD histogram
    if macd is not None:
        total += 1
        _line, _sig, histogram = macd
        if histogram > 0:
            bull_votes += 1
        elif histogram < 0:
            bear_votes += 1

    if bull_votes >= config.min_agreement and bull_votes > bear_votes:
        strength = config.trend_strength * (bull_votes / max(total, 1))
        return SubSignal(SignalDirection.LONG, strength, "trend")
    if bear_votes >= config.min_agreement and bear_votes > bull_votes:
        strength = config.trend_strength * (bear_votes / max(total, 1))
        return SubSignal(SignalDirection.SHORT, strength, "trend")

    return SubSignal(SignalDirection.NEUTRAL, 0.0, "trend")


def compute_vwap_signal(
    candles: CandleSeries, config: EnhancedSignalConfig
) -> SubSignal:
    """Detect price deviation from VWAP — mean-reversion to volume-weighted price.

    LONG when price is significantly below VWAP (oversold vs volume).
    SHORT when price is significantly above VWAP (overbought vs volume).
    Expects reversion to the volume-weighted mean.
    """
    if not config.vwap_enabled:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "vwap")

    n = config.vwap_period
    if len(candles.candles) < n:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "vwap")

    recent = candles.candles[-n:]
    # VWAP = sum(typical_price * volume) / sum(volume)
    cumulative_tp_vol = sum(c.typical_price * c.volume for c in recent)
    cumulative_vol = sum(c.volume for c in recent)

    if cumulative_vol <= 0:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "vwap")

    vwap = cumulative_tp_vol / cumulative_vol
    current_price = candles.candles[-1].close
    deviation = (current_price - vwap) / vwap if vwap > 0 else 0.0

    threshold = config.vwap_deviation_threshold
    if deviation < -threshold:
        return SubSignal(SignalDirection.LONG, config.breakout_strength * 0.8, "vwap")
    if deviation > threshold:
        return SubSignal(SignalDirection.SHORT, config.breakout_strength * 0.8, "vwap")

    return SubSignal(SignalDirection.NEUTRAL, 0.0, "vwap")


def compute_divergence_signal(
    candles: CandleSeries, config: EnhancedSignalConfig
) -> SubSignal:
    """Detect momentum divergence — price makes new extreme but RSI doesn't.

    LONG when price makes new low but RSI makes higher low (bullish divergence).
    SHORT when price makes new high but RSI makes lower high (bearish divergence).
    Strong reversal signal.
    """
    if not config.divergence_enabled:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "divergence")

    lookback = config.divergence_lookback
    rsi_period = config.rsi_period
    min_candles = lookback + rsi_period + 1

    if len(candles.candles) < min_candles:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "divergence")

    closes = candles.closes

    # Split into two halves of lookback
    half = lookback // 2
    first_half_closes = closes[-(lookback + 1):-half]
    second_half_closes = closes[-half - 1:]

    if len(first_half_closes) < rsi_period + 1 or len(second_half_closes) < rsi_period + 1:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "divergence")

    # Need enough context for RSI calculation
    full_first = closes[:-(half + 1)]
    full_second = closes[:]

    rsi_first = _calculate_rsi(full_first, rsi_period)
    rsi_second = _calculate_rsi(full_second, rsi_period)

    if rsi_first is None or rsi_second is None:
        return SubSignal(SignalDirection.NEUTRAL, 0.0, "divergence")

    price_first_low = min(first_half_closes)
    price_second_low = min(second_half_closes)
    price_first_high = max(first_half_closes)
    price_second_high = max(second_half_closes)

    threshold = config.divergence_rsi_threshold

    # Bullish divergence: price makes lower low but RSI makes higher low
    if price_second_low < price_first_low and rsi_second > rsi_first + threshold:
        return SubSignal(SignalDirection.LONG, config.mean_reversion_strength, "divergence")

    # Bearish divergence: price makes higher high but RSI makes lower high
    if price_second_high > price_first_high and rsi_second < rsi_first - threshold:
        return SubSignal(SignalDirection.SHORT, config.mean_reversion_strength, "divergence")

    return SubSignal(SignalDirection.NEUTRAL, 0.0, "divergence")

    closes = candles.closes

    # Calculate indicators
    ema_fast = _calculate_ema(closes, config.ema_fast)
    ema_slow = _calculate_ema(closes, config.ema_slow)
    rsi = _calculate_rsi(closes, config.rsi_period)
    macd = _calculate_macd(closes, config.macd_fast, config.macd_slow, config.macd_signal)

    bull_votes = 0
    bear_votes = 0
    total = 0

    # EMA trend
    if ema_fast is not None and ema_slow is not None:
        total += 1
        if ema_fast > ema_slow:
            bull_votes += 1
        elif ema_fast < ema_slow:
            bear_votes += 1

    # RSI momentum (above 50 = bullish, below 50 = bearish)
    if rsi is not None:
        total += 1
        if rsi > 50:
            bull_votes += 1
        elif rsi < 50:
            bear_votes += 1

    # MACD histogram
    if macd is not None:
        total += 1
        _line, _sig, histogram = macd
        if histogram > 0:
            bull_votes += 1
        elif histogram < 0:
            bear_votes += 1

    if bull_votes >= config.min_agreement and bull_votes > bear_votes:
        strength = config.trend_strength * (bull_votes / max(total, 1))
        return SubSignal(SignalDirection.LONG, strength, "trend")
    if bear_votes >= config.min_agreement and bear_votes > bull_votes:
        strength = config.trend_strength * (bear_votes / max(total, 1))
        return SubSignal(SignalDirection.SHORT, strength, "trend")

    return SubSignal(SignalDirection.NEUTRAL, 0.0, "trend")


def generate_enhanced_signal(
    symbol: str,
    candles: CandleSeries,
    config: EnhancedSignalConfig,
) -> Signal:
    """Generate combined signal from breakout, mean-reversion, trend, VWAP, and divergence.

    Union logic: any sub-signal fires → trade.
    If multiple sub-signals agree, strength is boosted.
    Signal source tracked in metadata for diagnostics.
    """
    breakout = compute_breakout_signal(candles, config)
    mean_rev = compute_mean_reversion_signal(candles, config)
    trend = compute_trend_signal(candles, config)
    vwap = compute_vwap_signal(candles, config)
    divergence = compute_divergence_signal(candles, config)

    # Collect non-neutral sub-signals
    active = [s for s in (breakout, mean_rev, trend, vwap, divergence) if s.direction != SignalDirection.NEUTRAL]

    if not active:
        return Signal(
            symbol=symbol,
            exchange=candles.exchange or "kucoin",
            direction=SignalDirection.NEUTRAL,
            strength=0.0,
            confidence=0.0,
            strategy_id="enhanced",
        )

    # Check for direction agreement
    longs = [s for s in active if s.direction == SignalDirection.LONG]
    shorts = [s for s in active if s.direction == SignalDirection.SHORT]

    # Conflict: both long and short signals → no trade
    if longs and shorts:
        return Signal(
            symbol=symbol,
            exchange=candles.exchange or "kucoin",
            direction=SignalDirection.NEUTRAL,
            strength=0.0,
            confidence=0.0,
            strategy_id="enhanced",
        )

    # Unanimous direction
    direction = SignalDirection.LONG if longs else SignalDirection.SHORT
    agreeing = longs if longs else shorts

    # Strength: max of agreeing signals, with synergy bonus for multiple
    base_strength = max(s.strength for s in agreeing)
    synergy = 1.0 + 0.2 * (len(agreeing) - 1)  # 1.0, 1.2, 1.4
    final_strength = min(1.0, base_strength * synergy)

    # Confidence: fraction of signal types that agree
    total_types = 5  # breakout, mean_reversion, trend, vwap, divergence
    confidence = len(agreeing) / total_types

    sources = [s.source for s in agreeing]

    return Signal(
        symbol=symbol,
        exchange=candles.exchange or "kucoin",
        direction=direction,
        strength=final_strength,
        confidence=confidence,
        price=candles.closes[-1] if candles.closes else 0.0,
        strategy_id="enhanced",
        features={"signal_sources": sources},
    )
