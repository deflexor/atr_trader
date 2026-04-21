"""Zero-drawdown risk management layer.

Provides pre-trade drawdown filtering, regime-aware position sizing,
bootstrap-validated stop calculation, and drawdown budget tracking.

Inspired by fecon235/236 techniques:
- Gaussian Mixture Models for regime detection
- Boltzmann thermal weighting for position sizing
- Bootstrap simulation for stop validation
"""

from .regime_detector import RegimeDetector, MarketRegime, RegimeResult
from .pre_trade_filter import PreTradeDrawdownFilter, TradeEvaluation
from .boltzmann_sizer import BoltzmannPositionSizer
from .bootstrap_stops import BootstrapStopCalculator, BootstrapStopResult
from .drawdown_budget import DrawdownBudgetTracker, DrawdownBudgetConfig

__all__ = [
    "RegimeDetector",
    "MarketRegime",
    "RegimeResult",
    "PreTradeDrawdownFilter",
    "TradeEvaluation",
    "BoltzmannPositionSizer",
    "BootstrapStopCalculator",
    "BootstrapStopResult",
    "DrawdownBudgetTracker",
    "DrawdownBudgetConfig",
]
