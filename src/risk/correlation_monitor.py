"""ETH/BTC correlated asset monitor for leading risk signals.

Tracks ETH price alongside BTC to detect divergences where ETH is
dropping while BTC is flat — a leading indicator that BTC may follow.

Key insight from Phases 2 & 3: lagging indicators (regime detection,
velocity) arrive too late. By the time they fire, trailing stops have
already acted. ETH dropping before BTC is a LEADING signal — it can
trigger pre-emptive trailing stop tightening BEFORE the BTC drop.

Detection logic:
    1. ETH return over lookback window (rolling % change)
    2. BTC return over same window (for comparison)
    3. Divergence = ETH return minus BTC return (ETH falling faster)
    4. ETH/BTC ratio trend (declining ratio = ETH weakening vs BTC)
    5. Signal strength = how far divergence exceeds threshold

Actions:
    - TIGHTEN trailing stops (primary — uses proven mechanism)
    - Reduce position size (fallback — via partial close)

Default thresholds:
    ETH drops >0.8% while BTC is flat (within ±0.3%): mild warning
    ETH drops >1.6% while BTC is flat: strong warning
    ETH drops >2.5% while BTC is flat: extreme — reduce position
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum


class CorrelationRiskLevel(Enum):
    """Risk level from correlation monitoring."""

    NORMAL = "NORMAL"          # No divergence detected
    ELEVATED = "ELEVATED"      # ETH dropping faster than BTC — tighten stops
    HIGH = "HIGH"              # Significant divergence — tighten stops aggressively
    EXTREME = "EXTREME"        # ETH crashing while BTC flat — reduce position


@dataclass(frozen=True)
class CorrelationSignal:
    """Immutable correlation monitoring result.

    Attributes:
        risk_level: Current risk level from divergence detection.
        eth_return_pct: ETH % return over lookback window.
        btc_return_pct: BTC % return over lookback window.
        divergence_pct: ETH return minus BTC return. Negative = ETH weakening.
        eth_btc_ratio: Current ETH/BTC price ratio.
        ratio_trend: Change in ETH/BTC ratio over lookback (negative = declining).
        trailing_atr_multiplier: Suggested trailing stop multiplier (lower = tighter).
            None = use current trailing params (no change).
        position_reduce_fraction: Fraction of position to reduce (0.0 = none).
    """

    risk_level: CorrelationRiskLevel
    eth_return_pct: float
    btc_return_pct: float
    divergence_pct: float
    eth_btc_ratio: float
    ratio_trend: float
    trailing_atr_multiplier: float | None
    position_reduce_fraction: float


@dataclass
class CorrelationMonitorConfig:
    """Configuration for correlation monitoring.

    Attributes:
        lookback_candles: Number of candles to compute returns over.
        mild_divergence_pct: ETH must drop this % more than BTC for ELEVATED.
        strong_divergence_pct: ETH must drop this % more than BTC for HIGH.
        extreme_divergence_pct: ETH must drop this % more than BTC for EXTREME.
        btc_flat_threshold_pct: BTC is considered "flat" within this ±% range.
        trailing_tighten_elevated: Trailing ATR multiplier at ELEVATED (e.g. 0.75 = 25% tighter).
        trailing_tighten_high: Trailing ATR multiplier at HIGH.
        trailing_tighten_extreme: Trailing ATR multiplier at EXTREME.
        reduce_at_extreme: Fraction of position to reduce at EXTREME risk.
        min_samples: Minimum ETH candles before monitoring is active.
    """

    lookback_candles: int = 20
    mild_divergence_pct: float = -0.8
    strong_divergence_pct: float = -1.6
    extreme_divergence_pct: float = -2.5
    btc_flat_threshold_pct: float = 0.3
    trailing_tighten_elevated: float = 0.75
    trailing_tighten_high: float = 0.50
    trailing_tighten_extreme: float = 0.25
    reduce_at_extreme: float = 0.25
    min_samples: int = 5


def _compute_return(prices: deque[float]) -> float:
    """Compute % return from oldest to newest price. Pure function.

    Returns 0.0 for insufficient data.
    """
    if len(prices) < 2:
        return 0.0
    oldest = prices[0]
    newest = prices[-1]
    if oldest == 0.0:
        return 0.0
    return ((newest - oldest) / oldest) * 100.0


def _classify_divergence(
    divergence_pct: float,
    btc_return_pct: float,
    btc_flat_threshold: float,
    mild: float,
    strong: float,
    extreme: float,
) -> CorrelationRiskLevel:
    """Classify risk level from ETH/BTC divergence. Pure function.

    Divergence is negative when ETH is dropping relative to BTC.
    Only triggers when BTC is relatively flat (not already dropping).
    """
    # If BTC is also dropping hard, the divergence is less predictive
    # — BTC is already reacting, not leading
    btc_dropping = btc_return_pct < -btc_flat_threshold

    if divergence_pct <= extreme and not btc_dropping:
        return CorrelationRiskLevel.EXTREME
    if divergence_pct <= strong and not btc_dropping:
        return CorrelationRiskLevel.HIGH
    if divergence_pct <= mild and not btc_dropping:
        return CorrelationRiskLevel.ELEVATED
    return CorrelationRiskLevel.NORMAL


class CorrelationMonitor:
    """Tracks ETH/BTC divergence as a leading risk indicator.

    Maintains rolling windows of close prices for both assets.
    Computes returns, divergence, and ETH/BTC ratio trend each candle.

    Thread-unsafe by design — used inside the single-threaded backtest loop.
    """

    def __init__(self, config: CorrelationMonitorConfig | None = None) -> None:
        self._config = config or CorrelationMonitorConfig()
        self._btc_prices: deque[float] = deque(maxlen=self._config.lookback_candles + 1)
        self._eth_prices: deque[float] = deque(maxlen=self._config.lookback_candles + 1)
        self._eth_btc_ratios: deque[float] = deque(maxlen=self._config.lookback_candles + 1)

    def update_btc(self, close_price: float) -> None:
        """Record BTC close price for current candle.

        Args:
            close_price: BTC close price.
        """
        self._btc_prices.append(close_price)
        self._update_ratio()

    def update_eth(self, close_price: float) -> None:
        """Record ETH close price for current candle.

        Args:
            close_price: ETH close price.
        """
        self._eth_prices.append(close_price)
        self._update_ratio()

    def _update_ratio(self) -> None:
        """Update ETH/BTC ratio when both prices are available."""
        if self._eth_prices and self._btc_prices and self._btc_prices[-1] > 0:
            ratio = self._eth_prices[-1] / self._btc_prices[-1]
            self._eth_btc_ratios.append(ratio)

    def evaluate(self) -> CorrelationSignal:
        """Compute correlation signal from current price windows.

        Returns NORMAL signal if insufficient data.
        """
        eth_return = _compute_return(self._eth_prices)
        btc_return = _compute_return(self._btc_prices)
        divergence = eth_return - btc_return

        # Current ETH/BTC ratio
        current_ratio = self._eth_btc_ratios[-1] if self._eth_btc_ratios else 0.0

        # Ratio trend: change over the lookback window
        ratio_trend = _compute_return(self._eth_btc_ratios) if len(self._eth_btc_ratios) >= 2 else 0.0

        # Classify risk level
        risk_level = _classify_divergence(
            divergence_pct=divergence,
            btc_return_pct=btc_return,
            btc_flat_threshold=self._config.btc_flat_threshold_pct,
            mild=self._config.mild_divergence_pct,
            strong=self._config.strong_divergence_pct,
            extreme=self._config.extreme_divergence_pct,
        )

        # Determine trailing stop multiplier and position reduction
        trailing_mult: float | None = None
        reduce_frac = 0.0

        if risk_level == CorrelationRiskLevel.ELEVATED:
            trailing_mult = self._config.trailing_tighten_elevated
        elif risk_level == CorrelationRiskLevel.HIGH:
            trailing_mult = self._config.trailing_tighten_high
        elif risk_level == CorrelationRiskLevel.EXTREME:
            trailing_mult = self._config.trailing_tighten_extreme
            reduce_frac = self._config.reduce_at_extreme

        return CorrelationSignal(
            risk_level=risk_level,
            eth_return_pct=eth_return,
            btc_return_pct=btc_return,
            divergence_pct=divergence,
            eth_btc_ratio=current_ratio,
            ratio_trend=ratio_trend,
            trailing_atr_multiplier=trailing_mult,
            position_reduce_fraction=reduce_frac,
        )

    def reset(self) -> None:
        """Clear all tracking data."""
        self._btc_prices.clear()
        self._eth_prices.clear()
        self._eth_btc_ratios.clear()

    @property
    def has_sufficient_data(self) -> bool:
        """Whether enough data has been accumulated for valid signals."""
        return len(self._eth_prices) >= self._config.min_samples and len(self._btc_prices) >= self._config.min_samples
