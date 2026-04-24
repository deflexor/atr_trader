# Next Session: Zero-Drawdown Risk Layer — Phase 4+

## Current State

Phase 1 risk layer is committed on branch `news`. Phase 2 (Approach C: Adaptive Position Sizing), Phase 3 (Approach D: Velocity-Based Sizing), and Phase 4 (Approach B: Correlation Monitoring) are all implemented and tested.

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
- **CorrelationMonitor** (NEW): ETH/BTC divergence detection as leading risk indicator
- **Position.reduce_entries()**: FIFO partial close support on the Position model
- **Engine._partial_close_position()**: Partial close with slippage, PnL tracking, budget recording

### What's Integrated
- Risk layer wired into `BacktestEngine` via `use_zero_drawdown_layer=True`
- Adaptive sizer wired via `use_adaptive_sizing=True` (enabled by default)
- Velocity sizer wired via `use_velocity_sizing=True` (enabled by default)
- Correlation monitor wired via `use_correlation_monitor=True` (enabled by default)
- ETH secondary candles passed via `secondary_candles` parameter to `engine.run()`
- Correlation signal tightens trailing stops (Approach E synergy) via `_get_trailing_params()`
- Correlation signal reduces positions at EXTREME risk level
- Regime-adaptive trailing stops (tighter during CRASH only)
- `BacktestConfig` extended with all risk layer + adaptive sizing + velocity sizing + correlation params
- `config/base.yaml` has all settings
- `Signal` model has `regime`, `risk_verdict`, `bootstrap_stop_pct` fields

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

**Correlation monitor signal stats (full 44k candle dataset):**
- lookback=20, mild=-0.8%, strong=-1.6%, extreme=-2.5%
- ELEVATED: 121 signals, HIGH: 10 signals, EXTREME: 0 signals
- The monitor correctly detects ETH/BTC divergences (131 events)

**Key findings:**
1. **Correlation monitor detects divergences correctly** — 131 ELEVATED+ events in 30 days
2. **Trailing stop tightening doesn't change outcomes** — same DD, same closes as Phase 3
3. **Same fundamental issue** — trailing stops are already the most effective DD control
4. **Marginal ATR tightening is insufficient** — going from 4x ATR to 3x ATR doesn't change the close price enough
5. **The monitor IS a leading signal** — it tightens BEFORE BTC drops, which is the right direction
6. **Infrastructure is valuable** — ETH data pipeline, correlation monitoring, and the framework work correctly

**CorrelationMonitor Implementation:**
- Tracks ETH and BTC close prices in rolling windows (default: 20 candles)
- Computes ETH return, BTC return, divergence (ETH - BTC), ETH/BTC ratio trend
- Classifies risk: NORMAL → ELEVATED → HIGH → EXTREME
- Primary action: Tightens trailing stops (multiplies ATR params)
  - ELEVATED: 0.75x trailing (25% tighter)
  - HIGH: 0.50x trailing (50% tighter)
  - EXTREME: 0.25x trailing (75% tighter) + 25% position reduction
- Smart suppression: If BTC is already dropping, divergence is less predictive → stays NORMAL
- ETH data pipeline: `engine.run()` accepts optional `secondary_candles` (ETH CandleSeries)
- ETH candles must be time-aligned with BTC candles (same timeframe and timestamps)

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

**Key findings:**
1. **Velocity sizer preserves win rate** — 66.7% (same as Phase 1) vs Phase 2's 60.0%
2. **Velocity sizer doesn't cut winners** — 0 false positive partial closes
3. **Phase 2's adaptive sizer made 1 unnecessary partial close** — cut a winner for -$1.66
4. **No drawdown improvement in current dataset** — trailing stops already handle all cases at 5.79% DD

### Why Velocity Didn't Trigger
The same fundamental issue applies: **trailing stops are too effective** for the velocity sizer to help. Positions that lose are closed by trailing stops before velocity builds up enough to trigger the thresholds. The velocity sizer is correctly conservative — it only activates when:
1. Market is in CRASH or VOLATILE_TRENDING regime
2. Position is losing (P&L < -0.3%)
3. P&L is dropping faster than -0.5%/candle (CRASH) or -0.3%/candle (VOLATILE)
4. Regime energy is above 0.3

These conditions are rarely met simultaneously in the current dataset.

## Phase 5: What Might Actually Work

### Approach A: Short Hedging (DIRECT offset)
When drawdown exceeds a threshold, open an opposing hedge position.

**Why it might work better than B/C/D:**
- Doesn't require reducing the original position (avoids cutting winners)
- Directly offsets losses with gains on the hedge
- Can be size-limited to cap the hedge risk

**Files:** `src/risk/hedge_manager.py` (new), `src/backtest/engine.py` (hedge signal generation)

### Approach E: Proactive Stop Tightening (DEEPER integration)
The correlation monitor already tightens trailing stops (Phase 4). This could go further:
- Combine multiple risk signals (velocity + correlation + regime) into a composite risk score
- Use the composite score to dynamically adjust trailing parameters
- Activate trailing stops earlier (lower activation ATR) when signals align

**Advantage:** Already partially implemented via correlation monitor. Just needs deeper integration.

**Files:** `src/backtest/engine.py` (composite risk score, dynamic trailing parameters)

### Approach B+: Enhanced Correlation Monitoring
Current Phase 4 implementation is basic. Could be improved:
- Cross-asset momentum: not just ETH, also track SOL, BNB as additional leading indicators
- Correlation breakdown: when correlation between ETH/BTC drops, it signals regime change
- Multi-timeframe correlation: 1m/5m/15m divergence for faster/slower signals

**Files:** `src/risk/correlation_monitor.py` (enhanced), multi-symbol data pipeline

## Recommended Priority
1. **Approach E** (Deeper Proactive Stop Tightening) — simpler, builds on Phase 4
2. **Approach B+** (Enhanced Correlation) — more data sources, more robust
3. **Approach A** (Short Hedging) — most complex, requires careful risk management

## Key Files Reference
- `src/risk/` — All risk modules
- `src/risk/adaptive_sizer.py` — Regime-aware graduated soft stops (AdaptiveSizerConfig, AdaptivePositionSizer)
- `src/risk/velocity_tracker.py` — Rolling-window P&L velocity computation (VelocityTracker, VelocityResult)
- `src/risk/velocity_sizer.py` — Velocity-based regime-aware sizer (VelocityPositionSizer, VelocitySizerConfig)
- `src/risk/correlation_monitor.py` — ETH/BTC divergence detection (CorrelationMonitor, CorrelationSignal, CorrelationMonitorConfig, CorrelationRiskLevel)
- `src/core/models/position.py` — Position model with `reduce_entries()` for partial close
- `src/backtest/engine.py` — Engine with `_partial_close_position()`, `_adaptive_sizer`, `_velocity_sizer`, `_velocity_tracker`, `_correlation_monitor`, `_secondary_candles` fields
- `src/backtest/engine.py` — `_update_positions_with_candle()` checks adaptive, velocity, AND correlation sizers each candle
- `src/backtest/engine.py` — `_get_trailing_params()` now tightens trailing stops based on correlation signal
- `src/backtest/engine.py` — `run()` accepts `secondary_candles` parameter for ETH data
- `config/base.yaml` — All risk config params including velocity sizing + correlation monitoring settings
- `tests/test_risk_smoke.py` — Smoke tests for all risk modules including `test_velocity_tracker()`, `test_velocity_sizer()`, `test_correlation_monitor()`
- `scripts/backtest/correlation_monitor_comparison.py` — Phase 1 vs Phase 3 vs Phase 4 comparison script
- `scripts/backtest/velocity_sizing_comparison.py` — Phase 1 vs Phase 2 vs Phase 3 comparison script
- `scripts/backtest/adaptive_sizing_comparison.py` — Phase 1 vs Phase 2 comparison script

## Key Lessons from Phases 2, 3 & 4
1. **Absolute P&L thresholds kill win rate** — normal BTC noise hits -1% to -3% routinely
2. **Velocity > level** — the rate of unrealized loss is more informative than the absolute level
3. **Velocity sizer is correctly conservative** — 0 false positives vs adaptive sizer's 1
4. **Trailing stops are the real DD control** — all positions are closed by trailing stops before any sizer can help
5. **The problem is detection, not action** — we know HOW to reduce/tighten; we need a LEADING signal
6. **Lagging indicators (regime, velocity) arrive too late** — by the time they fire, trailing stops have already acted
7. **Partial close infrastructure is valuable** — `reduce_entries()` and `_partial_close_position()` are reusable for hedging
8. **Leading indicators (ETH correlation) are the right direction** — they can act BEFORE BTC drops
9. **Tighten trailing > reduce position** — using the proven mechanism (trailing stops) is safer than new reduction code
10. **Smart suppression matters** — ETH/BTC divergence when BTC is already dropping is NOT predictive; only useful when BTC is flat

## Venv
```bash
/home/dfr/pypsik/.venv/bin/python  # Python 3.12 with numpy, scipy, torch
```
