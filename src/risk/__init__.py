"""Zero-drawdown risk management layer.

Provides pre-trade drawdown filtering, regime-aware position sizing,
bootstrap-validated stop calculation, drawdown budget tracking,
and adaptive intra-trade position reduction.

Inspired by fecon235/236 techniques:
- Gaussian Mixture Models for regime detection
- Boltzmann thermal weighting for position sizing
- Bootstrap simulation for stop validation
- Graduated soft stops for drawdown containment
"""

from .regime_detector import RegimeDetector, MarketRegime, RegimeResult
from .pre_trade_filter import PreTradeDrawdownFilter, TradeEvaluation
from .boltzmann_sizer import BoltzmannPositionSizer
from .bootstrap_stops import BootstrapStopCalculator, BootstrapStopResult
from .drawdown_budget import DrawdownBudgetTracker, DrawdownBudgetConfig
from .adaptive_sizer import AdaptivePositionSizer, AdaptiveSizerConfig

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
    "AdaptivePositionSizer",
    "AdaptiveSizerConfig",
]
