"""Enhanced strategy — async wrapper over pure signal functions.

Delegates to enhanced_signals module for actual signal computation.
Implements BaseStrategy interface so the backtest engine can use it
as a drop-in replacement for MomentumStrategy.
Includes adaptive position sizing that scales with recent win rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .base_strategy import BaseStrategy, StrategyConfig
from .enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal
from ..core.models.signal import Signal, SignalDirection
from ..core.models.candle import CandleSeries
from ..core.models.market_data import MarketData


@dataclass
class AdaptiveSizerConfig:
    """Configuration for adaptive position sizing."""

    base_risk: float = 0.03  # Base risk fraction (3%)
    min_risk: float = 0.01  # Minimum risk (1%)
    max_risk: float = 0.06  # Maximum risk (6%)
    streak_window: int = 10  # Look back N trades for win rate
    streak_bonus: float = 0.5  # Extra risk per 10% win rate above 50%
    streak_penalty: float = 0.3  # Risk reduction per 10% win rate below 50%


class AdaptiveSizer:
    """Scales position size based on recent trade outcomes.

    Winning streak → increase risk (up to max_risk).
    Losing streak → decrease risk (down to min_risk).
    """

    def __init__(self, config: Optional[AdaptiveSizerConfig] = None):
        self.config = config or AdaptiveSizerConfig()
        self._recent_pnls: list[float] = []

    def record_trade(self, pnl: float) -> None:
        """Record a closed trade's PnL."""
        self._recent_pnls.append(pnl)
        window = self.config.streak_window
        if len(self._recent_pnls) > window * 2:
            self._recent_pnls = self._recent_pnls[-window * 2:]

    @property
    def win_rate(self) -> float:
        """Win rate over recent streak_window trades."""
        recent = self._recent_pnls[-self.config.streak_window:]
        if not recent:
            return 0.5  # Default: no edge information
        wins = sum(1 for p in recent if p > 0)
        return wins / len(recent)

    @property
    def current_risk(self) -> float:
        """Current risk fraction based on recent performance."""
        wr = self.win_rate
        base = self.config.base_risk

        if wr > 0.5:
            # Scale up: +streak_bonus per 10% above 50%
            excess = (wr - 0.5) * 10  # 0-5 range
            risk = base + excess * self.config.streak_bonus * base
        elif wr < 0.5:
            # Scale down: -streak_penalty per 10% below 50%
            deficit = (0.5 - wr) * 10  # 0-5 range
            risk = base - deficit * self.config.streak_penalty * base
        else:
            risk = base

        return max(self.config.min_risk, min(self.config.max_risk, risk))

    @property
    def trade_count(self) -> int:
        return len(self._recent_pnls)


class EnhancedStrategy(BaseStrategy):
    """Multi-signal strategy with adaptive position sizing.

    Combines breakout, mean-reversion, trend, VWAP, and divergence signals.
    Uses union logic: any sub-signal type can trigger a trade.
    Tracks signal sources in metadata for diagnostics.
    Scales position size based on recent win/loss streak.
    """

    def __init__(self, config: Optional[EnhancedSignalConfig] = None,
                 sizer_config: Optional[AdaptiveSizerConfig] = None):
        super().__init__(StrategyConfig(name="enhanced"))
        self.enhanced_config = config or EnhancedSignalConfig()
        self.sizer = AdaptiveSizer(sizer_config)
        self.diagnostics: dict[str, int] = {
            "total_evaluated": 0,
            "signals_produced": 0,
        }

    async def generate_signal(
        self,
        symbol: str,
        candles: CandleSeries,
        market_data: Optional[MarketData] = None,
    ) -> Signal:
        """Generate enhanced trading signal via pure functions."""
        self.diagnostics["total_evaluated"] += 1
        signal = generate_enhanced_signal(symbol, candles, self.enhanced_config)

        if signal.direction != SignalDirection.NEUTRAL:
            self.diagnostics["signals_produced"] += 1
            # Attach adaptive risk to signal metadata
            signal.features = signal.features or {}
            signal.features["adaptive_risk"] = self.sizer.current_risk
            signal.features["win_rate"] = self.sizer.win_rate

        return signal

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        risk_per_trade: float = 0.02,
    ) -> float:
        """Calculate position size with adaptive risk based on win streak."""
        if signal.direction == SignalDirection.NEUTRAL or signal.price <= 0:
            return 0.0

        # Use adaptive risk if available, otherwise fallback
        adaptive_risk = signal.features.get("adaptive_risk", risk_per_trade) if signal.features else risk_per_trade
        risk_amount = portfolio_value * adaptive_risk
        adjusted_risk = risk_amount * max(signal.strength, 0.3)
        return adjusted_risk / signal.price

    def record_trade(self, pnl: float) -> None:
        """Record a closed trade for adaptive sizing."""
        self.sizer.record_trade(pnl)
