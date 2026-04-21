"""Pre-trade drawdown filter.

Before executing a trade, simulates worst-case P&L given the current
regime and volatility. Rejects trades that would exceed the per-trade
drawdown budget.

Uses regime-specific multipliers to estimate worst-case loss:
- CALM_TRENDING: Low multiplier (1.5x ATR)
- VOLATILE_TRENDING: High multiplier (3.0x ATR)
- MEAN_REVERTING: Medium multiplier (2.0x ATR)
- CRASH: Infinite multiplier (reject all new entries)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .regime_detector import MarketRegime, RegimeResult
from .drawdown_budget import DrawdownBudgetTracker


class TradeVerdict(Enum):
    """Pre-trade filter verdict."""

    APPROVED = "APPROVED"
    REJECTED_BUDGET = "REJECTED_BUDGET"
    REJECTED_REGIME = "REJECTED_REGIME"
    REJECTED_WORST_CASE = "REJECTED_WORST_CASE"


@dataclass(frozen=True)
class TradeEvaluation:
    """Immutable pre-trade evaluation result."""

    verdict: TradeVerdict
    estimated_worst_loss: float  # Currency units
    estimated_drawdown_pct: float  # % of capital
    regime: MarketRegime
    risk_multiplier: float  # Regime-specific risk multiplier
    reason: str = ""


# Regime-specific risk multipliers for worst-case estimation
REGIME_RISK_MULTIPLIERS: dict[MarketRegime, float] = {
    MarketRegime.CALM_TRENDING: 1.5,
    MarketRegime.VOLATILE_TRENDING: 3.0,
    MarketRegime.MEAN_REVERTING: 2.0,
    MarketRegime.CRASH: float("inf"),  # Block all entries
}


def _estimate_worst_case_loss(
    position_value: float,
    atr_pct: float,
    risk_multiplier: float,
) -> float:
    """Estimate worst-case loss for a position. Pure function.

    Args:
        position_value: Notional value of the position
        atr_pct: ATR as percentage of price
        risk_multiplier: Regime-specific multiplier

    Returns:
        Estimated worst-case loss in currency units
    """
    if risk_multiplier == float("inf"):
        return position_value  # Could lose everything

    return position_value * atr_pct * risk_multiplier


class PreTradeDrawdownFilter:
    """Evaluates trades before execution to prevent drawdown budget violations.

    Combines regime detection with budget tracking to reject trades
    that would likely exceed drawdown limits.
    """

    def __init__(
        self,
        budget_tracker: DrawdownBudgetTracker,
        max_per_trade_dd_pct: float = 0.01,
    ) -> None:
        self._budget = budget_tracker
        self._max_per_trade_dd_pct = max_per_trade_dd_pct

    def evaluate(
        self,
        regime_result: RegimeResult,
        position_value: float,
        capital: float,
        atr_pct: float = 0.01,
    ) -> TradeEvaluation:
        """Evaluate whether a proposed trade should be executed.

        Args:
            regime_result: Current regime classification
            position_value: Notional value of the proposed position
            capital: Current capital
            atr_pct: ATR as percentage of current price

        Returns:
            TradeEvaluation with verdict and risk metrics
        """
        regime = regime_result.regime
        risk_mult = REGIME_RISK_MULTIPLIERS.get(regime, 2.0)

        # CRASH regime: reject all new entries
        if regime == MarketRegime.CRASH:
            return TradeEvaluation(
                verdict=TradeVerdict.REJECTED_REGIME,
                estimated_worst_loss=position_value,
                estimated_drawdown_pct=position_value / capital if capital > 0 else 1.0,
                regime=regime,
                risk_multiplier=risk_mult,
                reason="CRASH regime: all new entries blocked",
            )

        # Estimate worst-case loss
        worst_loss = _estimate_worst_case_loss(position_value, atr_pct, risk_mult)
        dd_pct = worst_loss / capital if capital > 0 else 1.0

        # Check per-trade drawdown limit
        if dd_pct > self._max_per_trade_dd_pct:
            return TradeEvaluation(
                verdict=TradeVerdict.REJECTED_WORST_CASE,
                estimated_worst_loss=worst_loss,
                estimated_drawdown_pct=dd_pct,
                regime=regime,
                risk_multiplier=risk_mult,
                reason=f"Estimated DD {dd_pct:.2%} exceeds per-trade limit {self._max_per_trade_dd_pct:.2%}",
            )

        # Check budget tracker
        if not self._budget.can_enter_trade(estimated_loss=worst_loss):
            return TradeEvaluation(
                verdict=TradeVerdict.REJECTED_BUDGET,
                estimated_worst_loss=worst_loss,
                estimated_drawdown_pct=dd_pct,
                regime=regime,
                risk_multiplier=risk_mult,
                reason="Drawdown budget insufficient for this trade",
            )

        return TradeEvaluation(
            verdict=TradeVerdict.APPROVED,
            estimated_worst_loss=worst_loss,
            estimated_drawdown_pct=dd_pct,
            regime=regime,
            risk_multiplier=risk_mult,
        )
