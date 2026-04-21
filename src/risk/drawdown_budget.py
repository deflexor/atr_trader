"""Drawdown budget tracker.

Allocates a per-session drawdown budget and tracks consumption.
Each trade gets a per-trade drawdown allowance; when the total
budget is exhausted, all new entries are halted.

This replaces the simple max_drawdown_pct halt in BacktestEngine
with a more granular approach: instead of halting at 5% total DD,
we allocate budget per-trade and can reject individual trades
before they would push us over budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DrawdownBudgetConfig:
    """Immutable configuration for drawdown budget tracking."""

    total_budget_pct: float = 0.03  # 3% total drawdown budget per session
    per_trade_budget_pct: float = 0.01  # 1% max drawdown per trade
    recovery_threshold_pct: float = 0.5  # Resume at 50% of budget consumed
    cooldown_after_halt: int = 10  # Candles to wait after halt before resuming


@dataclass
class DrawdownBudgetTracker:
    """Tracks cumulative drawdown budget consumption.

    Allocates per-trade drawdown allowances from a total budget.
    When budget is exhausted, halts all new entries.
    Resumes when equity recovers past the recovery threshold.

    Stateful — maintains running budget and halt state.
    """

    config: DrawdownBudgetConfig = field(default_factory=DrawdownBudgetConfig)
    initial_capital: float = 10000.0
    peak_equity: float = field(default=None)  # type: ignore[assignment]
    current_equity: float = field(default=None)  # type: ignore[assignment]
    consumed_budget: float = 0.0  # PnL loss consumed from budget
    is_halted: bool = False
    halt_candle: int = -999
    candles_since_halt: int = 0

    def __post_init__(self) -> None:
        if self.peak_equity is None:
            self.peak_equity = self.initial_capital
        if self.current_equity is None:
            self.current_equity = self.initial_capital

    @property
    def total_budget(self) -> float:
        """Total drawdown budget in currency units."""
        return self.initial_capital * self.config.total_budget_pct

    @property
    def per_trade_budget(self) -> float:
        """Per-trade drawdown budget in currency units."""
        return self.initial_capital * self.config.per_trade_budget_pct

    @property
    def budget_remaining(self) -> float:
        """Remaining budget in currency units."""
        return max(0.0, self.total_budget - self.consumed_budget)

    @property
    def budget_consumed_pct(self) -> float:
        """Budget consumed as fraction of total (0-1)."""
        if self.total_budget <= 0:
            return 1.0
        return min(1.0, self.consumed_budget / self.total_budget)

    def update_equity(self, equity: float, candle_index: int = 0) -> None:
        """Update equity tracking and check halt/resume conditions."""
        self.current_equity = equity
        self.peak_equity = max(self.peak_equity, equity)

        # Compute drawdown from peak
        if self.peak_equity > 0:
            drawdown = self.peak_equity - equity
            self.consumed_budget = max(self.consumed_budget, drawdown)

        # Check halt condition
        if self.consumed_budget >= self.total_budget and not self.is_halted:
            self.is_halted = True
            self.halt_candle = candle_index

        # Check resume condition
        if self.is_halted:
            self.candles_since_halt = candle_index - self.halt_candle
            recovery_level = self.total_budget * self.config.recovery_threshold_pct
            if self.consumed_budget <= recovery_level:
                if self.candles_since_halt >= self.config.cooldown_after_halt:
                    self.is_halted = False

    def can_enter_trade(self, estimated_loss: float = 0.0) -> bool:
        """Check if a new trade is allowed given budget constraints.

        Args:
            estimated_loss: Worst-case loss estimate for the trade

        Returns:
            True if trade is within budget, False if it would exceed budget
        """
        if self.is_halted:
            return False

        # Check if estimated loss would exceed remaining budget
        if estimated_loss > 0 and estimated_loss > self.budget_remaining:
            return False

        # Check per-trade limit
        if estimated_loss > self.per_trade_budget:
            return False

        return True

    def record_trade_pnl(self, pnl: float) -> None:
        """Record realized PnL from a closed trade.

        Positive PnL reduces consumed budget (recovery).
        Negative PnL increases consumed budget.
        """
        if pnl < 0:
            self.consumed_budget += abs(pnl)
        else:
            # Wins can partially restore budget
            self.consumed_budget = max(0.0, self.consumed_budget - pnl * 0.5)

    def reset(self, initial_capital: float | None = None) -> None:
        """Reset tracker for new session."""
        if initial_capital is not None:
            self.initial_capital = initial_capital
        self.peak_equity = self.initial_capital
        self.current_equity = self.initial_capital
        self.consumed_budget = 0.0
        self.is_halted = False
        self.halt_candle = -999
        self.candles_since_halt = 0
