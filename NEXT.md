# Next Session: Zero-Drawdown Risk Layer — Phase 6+

## Current State

Phases 1-5 of the zero-drawdown risk layer are committed on branch `news`. All risk modules are implemented and tested.

### What's Implemented (`src/risk/`)
- **RegimeDetector**: GMM-inspired regime classification (CALM_TRENDING, VOLATILE_TRENDING, MEAN_REVERTING, CRASH)
- **PreTradeDrawdownFilter**: Rejects trades exceeding per-trade budget or during CRASH regime
- **BoltzmannPositionSizer**: Thermal weighting that de-risks in uncertain regimes
- **BootstrapStopCalculator**: Bootstrapped worst-case stop distances (available but currently disabled)
- **DrawdownBudgetTracker**: Cumulative per-session drawdown budget with halt/resume
- **Vol-spike detection**: Reduces position 50% when current ATR is 2x+ the 100-candle average
- **AdaptivePositionSizer**: Regime-aware graduated soft stops based on absolute P&L level
- **VelocityTracker**: Rolling-window unrealized P&L velocity computation via linear regression
- **VelocityPositionSizer**: Regime-aware velocity-threshold sizer (rate-of-loss detection)
- **CorrelationMonitor**: ETH/BTC divergence detection as leading risk indicator
- **CompositeRiskScorer** (NEW): Unified 0-1 score from regime + velocity + correlation with synergy bonus
- **Position.reduce_entries()**: FIFO partial close support on the Position model
- **Engine._partial_close_position()**: Partial close with slippage, PnL tracking, budget recording

### What's Integrated
- Risk layer wired into `BacktestEngine` via `use_zero_drawdown_layer=True`
- Adaptive sizer wired via `use_adaptive_sizing=True` (enabled by default)
- Velocity sizer wired via `use_velocity_sizing=True` (enabled by default)
- Correlation monitor wired via `use_correlation_monitor=True` (enabled by default)
- Composite risk scorer wired via `use_composite_risk=True` (enabled by default)
- ETH secondary candles passed via `secondary_candles` parameter to `engine.run()`
- Composite score drives trailing stop tightening + earlier activation when signals align
- Composite score drives position reduction (replaces 3 independent sizer checks)
- `BacktestConfig` extended with all risk layer + adaptive sizing + velocity sizing + correlation + composite risk params
- `config/base.yaml` has all settings
- `Signal` model has `regime`, `risk_verdict`, `bootstrap_stop_pct` fields

### Phase 5 Implementation (Approach E: Composite Risk Score)

**New module: `src/risk/composite_risk_scorer.py`**

A pure function that takes inputs from all 3 risk subsystems and produces a unified 0-1 composite score:

- **Sub-score normalization**:
  - Regime: CRASH=1.0, VOLATILE=0.6, MEAN_REVERTING=0.2, CALM=0.0 (scaled by energy)
  - Velocity: |velocity| / max_velocity (capped at 1.0)
  - Correlation: NORMAL=0.0, ELEVATED=0.4, HIGH=0.7, EXTREME=1.0
- **Weighted combination**: regime (0.35) + velocity (0.30) + correlation (0.35)
- **Synergy bonus**: When 2+ sub-scores >= 0.3, apply multiplicative bonus (default 1.4x)
  - CRASH + HIGH correlation alone -> score ~0.28; together -> score ~0.74 (synergy!)
- **Primary output - Trailing stop adjustment**:
  - Score >= 0.3: Begin tightening (trailing multiplier decreases linearly to 0.3x at score=1.0)
  - Score >= 0.3: Earlier activation (reduce activation ATR by up to 15% per 0.1 score)
- **Secondary output - Position reduction**:
  - Score >= 0.6: Begin reducing (linearly up to 50% at score=1.0)

**Engine integration:**
- `_get_trailing_params()`: When composite risk enabled, uses composite score's trailing multiplier + activation reduction instead of correlation-only multiplier
- `_update_positions_with_candle()`: When composite risk enabled, uses composite score's `position_reduce_fraction` instead of 3 independent sizer checks
- Velocity tracker still updated every candle (data accumulation needed for composite score computation)

**Key innovation - Synergy:**
The composite scorer captures that aligned signals are more predictive than any single signal:
- CRASH regime alone: score = 0.315
- HIGH correlation alone: score = 0.245
- CRASH + HIGH correlation: score = 0.739 (NOT 0.560 = linear sum; synergy bonus!)
- All three extreme: score > 0.9 with position reduction + very tight trailing + early activation

**Backtest comparison script:** `scripts/backtest/composite_risk_comparison.py`

### Phase 4 Backtest Results (Approach B: Correlation Monitoring)

**30-day 5m backtest comparison (includes Feb 1 crash):**
```
                   Phase 1     Phase 3 (Velocity)   Phase 4 (Correlation)
Return:           +0.25%      +0.23%                +0.23%
Max Drawdown:      6.77%       6.77%                 6.77%
Sharpe:           5440        4637                  4033
Win Rate:          54%         50%                   50%
Partial Closes:     0           1                     1
```

### Phase 3 Backtest Results (Approach D: Velocity-Based Sizing)

**60-day 5m backtest comparison:**
```
                Phase 1     Phase 2 (Adaptive)   Phase 3 (Velocity)
Return:        +0.24%      +0.22%               +0.24%
Max Drawdown:   5.79%       5.79%                5.79%
Sharpe:       6113.97     6376.50              6399.11
Win Rate:      66.7%       60.0%                66.7%
Partial Closes: 0          1                    0
```

## Phase 6: What Might Actually Work

### Approach A: Short Hedging (DIRECT offset)
When drawdown exceeds a threshold, open an opposing hedge position.

**Why it might work better than B/C/D/E:**
- Doesn't require reducing the original position (avoids cutting winners)
- Directly offsets losses with gains on the hedge
- Can be size-limited to cap the hedge risk

**Files:** `src/risk/hedge_manager.py` (new), `src/backtest/engine.py` (hedge signal generation)

### Approach E+: Deeper Composite Score Integration
Current Phase 5 composite score drives trailing params and position reduction. Could go further:
- **Adaptive weights**: Learn from backtest which signal is most predictive for this market
- **Regime-conditioned scoring**: Different weight profiles per regime
- **Time-decay**: Recent signals weighted more heavily
- **Signal confirmation**: Require persistence (2+ consecutive elevated readings) before acting

**Files:** `src/risk/composite_risk_scorer.py` (enhanced), engine integration

### Approach B+: Enhanced Correlation Monitoring
- Cross-asset momentum: not just ETH, also track SOL, BNB as additional leading indicators
- Correlation breakdown: when correlation between ETH/BTC drops, it signals regime change
- Multi-timeframe correlation: 1m/5m/15m divergence for faster/slower signals

**Files:** `src/risk/correlation_monitor.py` (enhanced), multi-symbol data pipeline

### Approach F: Volatility Regime Switching
- Use realized vs implied volatility ratio as a risk indicator
- When realized vol exceeds implied -> market is stress-testing -> tighten
- ATR percentile already computed in regime detector -- use it directly

**Files:** `src/risk/volatility_switch.py` (new), engine integration

## Recommended Priority
1. **Approach E+** (Deeper Composite Score) -- builds directly on Phase 5, minimal new code
2. **Approach F** (Volatility Regime Switching) -- ATR percentile already computed, easy integration
3. **Approach A** (Short Hedging) -- most complex, requires careful risk management
4. **Approach B+** (Enhanced Correlation) -- more data sources, more robust

## Key Files Reference
- `src/risk/` -- All risk modules
- `src/risk/composite_risk_scorer.py` -- Composite risk scoring (CompositeRiskConfig, CompositeRiskScore, compute_composite_score)
- `src/risk/correlation_monitor.py` -- ETH/BTC divergence detection (CorrelationMonitor, CorrelationSignal, CorrelationMonitorConfig, CorrelationRiskLevel)
- `src/risk/velocity_sizer.py` -- Velocity-based regime-aware sizer (VelocityPositionSizer, VelocitySizerConfig)
- `src/risk/velocity_tracker.py` -- Rolling-window P&L velocity computation (VelocityTracker, VelocityResult)
- `src/risk/adaptive_sizer.py` -- Regime-aware graduated soft stops (AdaptiveSizerConfig, AdaptivePositionSizer)
- `src/risk/regime_detector.py` -- GMM-inspired regime classification (RegimeDetector, MarketRegime, RegimeResult)
- `src/core/models/position.py` -- Position model with `reduce_entries()` for partial close
- `src/backtest/engine.py` -- Engine with composite risk integration
- `config/base.yaml` -- All risk config params including composite risk settings
- `tests/test_risk_smoke.py` -- Smoke tests for all risk modules including `test_composite_risk_scorer()`
- `scripts/backtest/composite_risk_comparison.py` -- Phase 4 vs Phase 5 comparison script
- `scripts/backtest/correlation_monitor_comparison.py` -- Phase 1 vs Phase 3 vs Phase 4 comparison script

## Key Lessons from Phases 2, 3, 4 & 5
1. **Absolute P&L thresholds kill win rate** -- normal BTC noise hits -1% to -3% routinely
2. **Velocity > level** -- the rate of unrealized loss is more informative than the absolute level
3. **Velocity sizer is correctly conservative** -- 0 false positives vs adaptive sizer's 1
4. **Trailing stops are the real DD control** -- all positions are closed by trailing stops before any sizer can help
5. **The problem is detection, not action** -- we know HOW to reduce/tighten; we need a LEADING signal
6. **Lagging indicators (regime, velocity) arrive too late** -- by the time they fire, trailing stops have already acted
7. **Partial close infrastructure is valuable** -- `reduce_entries()` and `_partial_close_position()` are reusable for hedging
8. **Leading indicators (ETH correlation) are the right direction** -- they can act BEFORE BTC drops
9. **Tighten trailing > reduce position** -- using the proven mechanism (trailing stops) is safer than new reduction code
10. **Smart suppression matters** -- ETH/BTC divergence when BTC is already dropping is NOT predictive
11. **Synergy > individual signals** -- aligned signals (CRASH + correlation) are much stronger than either alone
12. **Composite score enables earlier activation** -- earlier trailing stop activation is a new lever not available to individual signals

## Venv
```bash
/home/dfr/pypsik/.venv/bin/python  # Python 3.12 with numpy, scipy, torch
```
