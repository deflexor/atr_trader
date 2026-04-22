# Next Session: Zero-Drawdown Risk Layer — Phase 2+

## Current State

Phase 1 risk layer is committed on branch `news`. Phase 2 Approach C (Adaptive Position Sizing) is implemented and tested.

### What's Implemented (`src/risk/`)
- **RegimeDetector**: GMM-inspired regime classification (CALM_TRENDING, VOLATILE_TRENDING, MEAN_REVERTING, CRASH)
- **PreTradeDrawdownFilter**: Rejects trades exceeding per-trade budget or during CRASH regime
- **BoltzmannPositionSizer**: Thermal weighting that de-risks in uncertain regimes
- **BootstrapStopCalculator**: Bootstrapped worst-case stop distances (available but currently disabled)
- **DrawdownBudgetTracker**: Cumulative per-session drawdown budget with halt/resume
- **Vol-spike detection**: Reduces position 50% when current ATR is 2x+ the 100-candle average
- **AdaptivePositionSizer** (NEW): Regime-aware graduated soft stops that reduce positions during dangerous regimes
- **Position.reduce_entries()** (NEW): FIFO partial close support on the Position model
- **Engine._partial_close_position()** (NEW): Partial close with slippage, PnL tracking, budget recording

### What's Integrated
- Risk layer wired into `BacktestEngine` via `use_zero_drawdown_layer=True`
- Adaptive sizer wired via `use_adaptive_sizing=True` (enabled by default)
- Regime-adaptive trailing stops (tighter during CRASH only)
- `BacktestConfig` extended with all risk layer + adaptive sizing params
- `config/base.yaml` has all settings
- `Signal` model has `regime`, `risk_verdict`, `bootstrap_stop_pct` fields

### Phase 2 Backtest Results (Approach C: Adaptive Position Sizing)

**Static thresholds (-1%/-2%/-3%)** — kills win rate:
```
               Phase 1     Static     Change
Return:        +0.65%     -0.01%     -0.66pp  (destroyed)
Win Rate:      60.0%       7.1%     -52.9pp  (catastrophic)
Max Drawdown:   2.79%      5.80%    +3.01pp   (worse!)
Partial closes: 0         76
```

**Regime-aware defaults (CRASH: -2%/-3%/-5%, VOLATILE: -3%/-5%/-8%)** — minimal impact:
```
               Phase 1     Regime-Aware   Change
Return:        +0.65%      +0.16%        -0.49pp
Win Rate:      60.0%       41.9%        -18.1pp
Max Drawdown:   2.79%       2.85%        +0.06pp  (negligible)
Partial closes: 0          12
```

**During Feb 5-6 crash week (-15% BTC)**:
```
               Phase 1     Regime-Aware   Change
Return:        -5.03%     -5.10%        -0.07pp
Max Drawdown:   5.44%      5.50%        +0.06pp
Partial closes: 0          2
```

### The Problem with Approach C
1. **Trailing stops already handle losers well** — losers close at -0.06% to -0.09%, so there's nothing for the adaptive sizer to improve
2. **The sizer cuts winners that dip then recover** — even regime-aware, it reduces during CRASH regime which sometimes fires during recoveries
3. **Regime detection is lagging** — CRASH is detected after the drop, by which time trailing stops have already closed positions
4. **The 9% drawdown from Phase 1 isn't in our dataset** — our data (Sep 2025 – Feb 2026) has a -15% crash, but trailing stops handle it at ~5.5% DD
5. **The adaptive sizer's value is defensive** — it's insurance for regime-detection failures, not a primary DD reducer

## Phase 3: What Might Actually Work

### Approach D: Velocity-Based Adaptive Sizing
Instead of thresholding on unrealized P&L level, threshold on the **rate of change** of unrealized P&L.

**Rationale**: A position at -2% that's been flat for hours is fine. A position that went from 0% to -2% in 3 candles is in trouble. The velocity (acceleration of loss) is the signal, not the absolute level.

**Implementation:**
- Track unrealized P&L per position over last N candles
- If unrealized P&L is dropping faster than X% per candle, trigger reduction
- This avoids cutting winners that are just in a normal pullback
- Could be combined with regime: only during CRASH/VOLATILE_TRENDING

**Files:** `src/risk/adaptive_sizer.py` (add velocity tracking), `src/backtest/engine.py` (pass P&L history)

### Approach A: Short Hedging (still viable)
When drawdown exceeds a threshold, open an opposing hedge position.

**Why it might work better than Approach C:**
- Doesn't require reducing the original position (avoids cutting winners)
- Directly offsets losses with gains on the hedge
- Can be size-limited to cap the hedge risk

**Files:** `src/risk/hedge_manager.py` (new), `src/backtest/engine.py` (hedge signal generation)

### Approach B: Correlated Asset Monitoring (still viable)
ETH often leads BTC drops. Early warning system.

**Why it might work:** It's a leading indicator, not a lagging one like regime detection. ETH dropping is a signal that BTC may follow, before it actually does.

**Files:** `src/risk/correlation_monitor.py` (new), multi-asset data pipeline

## Recommended Priority
1. **Approach D** (Velocity-Based Sizing) — enhancement to existing adaptive_sizer.py, doesn't require new modules
2. **Approach B** (Correlation Monitoring) — leading indicator, could actually prevent DD
3. **Approach A** (Short Hedging) — more complex, requires careful risk management

## Key Files Reference
- `src/risk/` — All risk modules including new `adaptive_sizer.py`
- `src/risk/adaptive_sizer.py` — Regime-aware graduated soft stops (AdaptiveSizerConfig, AdaptivePositionSizer)
- `src/core/models/position.py` — Position model with `reduce_entries()` for partial close
- `src/backtest/engine.py` — Engine with `_partial_close_position()`, `_adaptive_sizer` field
- `src/backtest/engine.py` — `_update_positions_with_candle()` now checks adaptive sizer each candle
- `config/base.yaml` — All risk config params including `adaptive_sizing`, `adaptive_cooldown_candles`, `adaptive_min_energy`
- `tests/test_risk_smoke.py` — Smoke tests for all risk modules including `test_adaptive_sizer()`, `test_position_reduce_entries()`
- `scripts/backtest/adaptive_threshold_sweep.py` — Threshold sweep comparison script
- `scripts/backtest/adaptive_sizing_comparison.py` — Phase 1 vs Phase 2 comparison script

## Key Lessons from Phase 2
1. **Static P&L thresholds kill win rate** — -1%/-2%/-3% is normal BTC noise, not danger
2. **Regime-aware filtering helps but barely activates** — CRASH is lagging; most of the time = MEAN_REVERTING (excluded)
3. **Trailing stops are already excellent** — losers close at -0.06% to -0.09%, no room for soft stops to help
4. **Partial close infrastructure is valuable** — `reduce_entries()` and `_partial_close_position()` work correctly and are reusable for hedging
5. **The problem is detection, not action** — we know HOW to reduce positions; we don't know WHEN (the signal is too laggy)
6. **Velocity > level** — the rate of unrealized loss is more informative than the absolute level

## Venv
```bash
/home/dfr/pypsik/.venv/bin/python  # Python 3.12 with numpy, scipy, torch
```
