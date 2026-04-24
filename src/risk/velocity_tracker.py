"""Rolling-window unrealized P&L velocity tracker.

Computes the rate of change (velocity) and acceleration of unrealized
P&L per position. Velocity = how fast losses are accumulating;
acceleration = whether the loss rate is itself speeding up.

Key insight from Phase 2: absolute P&L level is a poor trigger because
normal BTC noise causes -1% to -2% dips that recover. But a position
dropping -0.5%/candle for 5 consecutive candles is structurally
different from one that drifted to -2% over 50 candles.

The tracker maintains a fixed-length deque of (candle_idx, pnl_pct)
samples per position. Velocity is computed as the linear regression
slope over the window — more robust than simple difference because
it averages out single-candle noise.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VelocityResult:
    """Immutable velocity computation result.

    Attributes:
        velocity: Rate of P&L change (% per candle). Negative = losing.
        acceleration: Change in velocity (% per candle²). Negative = accelerating loss.
        window_size: Number of samples used in computation.
        current_pnl_pct: Most recent unrealized P&L %.
    """

    velocity: float
    acceleration: float
    window_size: int
    current_pnl_pct: float


@dataclass
class VelocityTrackerConfig:
    """Configuration for velocity tracking.

    Attributes:
        window_candles: Number of recent candles to compute velocity over.
        min_samples: Minimum samples before velocity is valid.
    """

    window_candles: int = 5
    min_samples: int = 3


class VelocityTracker:
    """Tracks unrealized P&L velocity per position using a rolling window.

    Thread-unsafe by design — used inside the single-threaded backtest loop.
    """

    def __init__(self, config: VelocityTrackerConfig | None = None) -> None:
        self._config = config or VelocityTrackerConfig()
        self._history: dict[int, deque[tuple[int, float]]] = {}

    def update(self, position_id: int, candle_idx: int, pnl_pct: float) -> None:
        """Record unrealized P&L for a position at a given candle.

        Args:
            position_id: Unique position identifier (id(position)).
            candle_idx: Current candle index in the backtest.
            pnl_pct: Current unrealized P&L % (negative = loss).
        """
        if position_id not in self._history:
            self._history[position_id] = deque(maxlen=self._config.window_candles)
        self._history[position_id].append((candle_idx, pnl_pct))

    def compute(self, position_id: int) -> VelocityResult | None:
        """Compute velocity and acceleration for a position.

        Returns None if insufficient samples.
        Velocity is computed via linear regression slope for robustness.
        Acceleration is the second derivative (velocity of velocity).
        """
        samples = self._history.get(position_id)
        if samples is None or len(samples) < self._config.min_samples:
            return None

        items = list(samples)
        n = len(items)
        candle_indices = [s[0] for s in items]
        pnl_values = [s[1] for s in items]

        # Linear regression: pnl = a * candle_idx + b
        # Velocity = slope (a) — change in P&L per candle
        velocity = _regression_slope(candle_indices, pnl_values)
        current_pnl = pnl_values[-1]

        # Acceleration: if enough samples, compute velocity on first half vs second half
        if n >= self._config.min_samples + 1:
            mid = n // 2
            v1 = _regression_slope(candle_indices[:mid + 1], pnl_values[:mid + 1])
            v2 = _regression_slope(candle_indices[mid:], pnl_values[mid:])
            dt = candle_indices[-1] - candle_indices[mid]
            acceleration = (v2 - v1) / dt if dt > 0 else 0.0
        else:
            acceleration = 0.0

        return VelocityResult(
            velocity=velocity,
            acceleration=acceleration,
            window_size=n,
            current_pnl_pct=current_pnl,
        )

    def remove(self, position_id: int) -> None:
        """Remove tracking data for a closed position."""
        self._history.pop(position_id, None)

    def reset(self) -> None:
        """Clear all tracking data."""
        self._history.clear()

    @property
    def tracked_positions(self) -> int:
        """Number of positions currently being tracked."""
        return len(self._history)


def _regression_slope(x: list[int], y: list[float]) -> float:
    """Compute linear regression slope. Pure function.

    Returns 0.0 for degenerate cases (constant x, single point).
    """
    n = len(x)
    if n < 2:
        return 0.0

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    denominator = sum((xi - mean_x) ** 2 for xi in x)

    if denominator == 0.0:
        return 0.0

    return numerator / denominator
