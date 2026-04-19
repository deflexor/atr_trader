"""Performance metrics calculation for backtesting.

Calculates Sharpe ratio, max drawdown, win rate, and other metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import math


@dataclass
class PerformanceMetrics:
    """Performance metrics calculator for trading strategies."""

    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_trade: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0

    def reset(self) -> None:
        """Reset all metrics to initial state."""
        self.max_drawdown = 0.0
        self.sharpe_ratio = 0.0
        self.sortino_ratio = 0.0
        self.calmar_ratio = 0.0
        self.win_rate = 0.0
        self.profit_factor = 0.0
        self.avg_trade = 0.0
        self.avg_win = 0.0
        self.avg_loss = 0.0
        self.largest_win = 0.0
        self.largest_loss = 0.0
        self.consecutive_wins = 0
        self.consecutive_losses = 0

    def calculate_from_trades(
        self,
        trades: list[dict],
        equity_curve: list[dict],
        risk_free_rate: float = 0.02,
    ) -> None:
        """Calculate metrics from trade history and equity curve.

        Args:
            trades: List of trade dictionaries with 'pnl' key
            equity_curve: List of equity curve points [{timestamp, equity}]
            risk_free_rate: Annual risk-free rate for Sharpe calculation

        """
        if not trades:
            return

        # Calculate drawdown from equity curve
        self._calculate_drawdown(equity_curve)

        # Calculate trade statistics
        pnls = [t.get("pnl", 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        self.win_rate = len(wins) / len(pnls) if pnls else 0

        if wins:
            self.avg_win = sum(wins) / len(wins)
            self.largest_win = max(wins)

        if losses:
            self.avg_loss = sum(losses) / len(losses)
            self.largest_loss = min(losses)  # Most negative

        self.avg_trade = sum(pnls) / len(pnls) if pnls else 0

        # Profit factor
        total_wins = sum(wins) if wins else 0
        total_losses = abs(sum(losses)) if losses else 0
        self.profit_factor = total_wins / total_losses if total_losses > 0 else 0

        # Derive annualization factor from equity curve timestamps
        trades_per_year = self._estimate_trades_per_year(trades)

        # Sharpe ratio
        self.sharpe_ratio = self._calculate_sharpe(pnls, risk_free_rate, trades_per_year)

        # Sortino ratio (downside deviation)
        self.sortino_ratio = self._calculate_sortino(pnls, risk_free_rate, trades_per_year)

        # Calmar ratio (return / max drawdown)
        if self.max_drawdown > 0:
            total_return = sum(pnls)
            self.calmar_ratio = total_return / self.max_drawdown

    def _estimate_trades_per_year(self, trades: list[dict]) -> float:
        """Estimate number of trades per year from close trade timestamps.

        Returns 252 as fallback if timestamps are unusable.
        """
        from datetime import datetime

        timestamps = []
        for t in trades:
            ts = t.get("timestamp")
            if ts is None:
                continue
            if isinstance(ts, datetime):
                timestamps.append(ts.timestamp())
            elif isinstance(ts, (int, float)):
                timestamps.append(float(ts))

        if len(timestamps) < 2:
            return 252.0

        first = min(timestamps)
        last = max(timestamps)
        span_seconds = last - first

        if span_seconds <= 0:
            return 252.0

        span_years = span_seconds / (365.25 * 24 * 3600)
        if span_years <= 0:
            return 252.0

        return len(timestamps) / span_years

    def _calculate_drawdown(self, equity_curve: list[dict]) -> None:
        """Calculate maximum drawdown from equity curve."""
        if not equity_curve:
            return

        peak = equity_curve[0]["equity"] if equity_curve else 0
        max_dd = 0.0

        for point in equity_curve:
            equity = point["equity"]
            if equity > peak:
                peak = equity
            drawdown = peak - equity
            if drawdown > max_dd:
                max_dd = drawdown

        self.max_drawdown = max_dd / peak * 100 if peak > 0 else 0.0

    def _calculate_sharpe(
        self, pnls: list[float], risk_free_rate: float, trades_per_year: float = 252.0
    ) -> float:
        """Calculate Sharpe ratio from PnL series.

        Args:
            pnls: Per-trade PnL values
            risk_free_rate: Annual risk-free rate
            trades_per_year: Estimated trades per year for annualization
        """
        if len(pnls) < 2:
            return 0.0

        import statistics

        avg_return = statistics.mean(pnls)
        std_return = statistics.stdev(pnls)

        if std_return == 0:
            return 0.0

        # Annualize using actual trade frequency
        sharpe = (
            (avg_return - risk_free_rate / trades_per_year)
            / std_return
            * math.sqrt(trades_per_year)
        )
        return sharpe

    def _calculate_sortino(
        self, pnls: list[float], risk_free_rate: float, trades_per_year: float = 252.0
    ) -> float:
        """Calculate Sortino ratio (uses downside deviation).

        Args:
            pnls: Per-trade PnL values
            risk_free_rate: Annual risk-free rate
            trades_per_year: Estimated trades per year for annualization
        """
        if len(pnls) < 2:
            return 0.0

        import statistics

        avg_return = statistics.mean(pnls)

        # Calculate downside deviations (only negative returns)
        negative_returns = [p for p in pnls if p < 0]
        if not negative_returns:
            return 0.0

        downside_std = statistics.stdev(negative_returns) if len(negative_returns) > 1 else 0

        if downside_std == 0:
            return 0.0

        # Annualize
        sortino = (
            (avg_return - risk_free_rate / trades_per_year)
            / downside_std
            * math.sqrt(trades_per_year)
        )
        return sortino

    def to_dict(self) -> dict:
        """Convert metrics to dictionary."""
        return {
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "calmar_ratio": self.calmar_ratio,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "avg_trade": self.avg_trade,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "largest_win": self.largest_win,
            "largest_loss": self.largest_loss,
        }

    def __str__(self) -> str:
        """String representation of metrics."""
        return (
            f"Sharpe: {self.sharpe_ratio:.2f}, "
            f"Max DD: {self.max_drawdown:.2f}, "
            f"Win Rate: {self.win_rate:.1%}, "
            f"Profit Factor: {self.profit_factor:.2f}"
        )
