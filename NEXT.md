# Next Session: Zero-Drawdown Risk Layer — Phase 2

## Current State

The zero-drawdown risk layer (Phase 1) is committed and live on branch `news`.

### What's Implemented (`src/risk/`)
- **RegimeDetector**: GMM-inspired regime classification (CALM_TRENDING, VOLATILE_TRENDING, MEAN_REVERTING, CRASH)
- **PreTradeDrawdownFilter**: Rejects trades exceeding per-trade budget or during CRASH regime
- **BoltzmannPositionSizer**: Thermal weighting that de-risks in uncertain regimes
- **BootstrapStopCalculator**: Bootstrapped worst-case stop distances (available but currently disabled)
- **DrawdownBudgetTracker**: Cumulative per-session drawdown budget with halt/resume
- **Vol-spike detection**: Reduces position 50% when current ATR is 2x+ the 100-candle average

### What's Integrated
- Risk layer wired into `BacktestEngine` via `use_zero_drawdown_layer=True`
- Regime-adaptive trailing stops (tighter during CRASH only)
- `BacktestConfig` extended with all risk layer params
- `config/base.yaml` has new zero-drawdown settings
- `Signal` model has `regime`, `risk_verdict`, `bootstrap_stop_pct` fields

### 60-Day BTC Backtest Results (Phase 1)
```
              Baseline    Zero-DD     Change
Return:       +2.04%     +1.42%     -0.61pp  (kept 70%)
Win Rate:     80.8%      75.5%      -5.3pp   (acceptable)
Max Drawdown:  8.99%      9.04%     ~same
Trades:        118        110
CRASH entries blocked: 11
Boltzmann reductions: 77
```

### The Problem
The 9% max drawdown comes from a **single sudden adverse move** (BTC $75K→$65K in late March). The regime detector showed energy=0.12 (very confident, low risk) right up until the crash. No pre-trade filter can prevent this — it's inherent market risk from being in a position when a sudden move happens.

## Phase 2: Three Approaches to Actually Reduce Drawdown

### Approach A: Short Hedging
When drawdown exceeds a threshold (e.g. 3%), open an opposing hedge position to limit further losses.

**Implementation:**
- Add `HedgeManager` to `src/risk/` that monitors open positions
- When unrealized drawdown on any position exceeds threshold, generate a counter-direction signal
- Hedge size = fraction of the losing position (e.g. 50%)
- Close hedge when original position recovers or trailing stop closes both

**Files to modify:** `src/risk/hedge_manager.py` (new), `src/backtest/engine.py` (hedge signal generation in candle loop)

**Expected impact:** Could cap per-trade drawdown at ~3-5% instead of 9%, at the cost of hedge commissions and some profit on reversals.

### Approach B: Correlated Asset Monitoring
Monitor ETH alongside BTC. If ETH drops, reduce BTC exposure before BTC follows.

**Implementation:**
- Add `CorrelationMonitor` that tracks cross-asset returns in real-time
- If ETH drops >1% in last N candles while we hold BTC long, trigger tighter trailing or partial close
- This works because crypto assets are highly correlated — ETH often leads BTC moves

**Files to modify:** `src/risk/correlation_monitor.py` (new), `src/backtest/engine.py` (multi-asset candle loop), `src/adapters/` (parallel data feed)

**Expected impact:** Early warning system for correlated crashes. Could reduce drawdown 20-40% during cross-asset selloffs.

### Approach C: Adaptive Position Sizing Based on Open P&L
Scale down positions dynamically as unrealized losses grow.

**Implementation:**
- Track unrealized P&L per position on each candle
- If a position goes negative, reduce its size by closing a fraction
- Use a graduated scale: -1% → reduce 25%, -2% → reduce 50%, -3% → close entirely
- This is like a "soft stop" that gradually exits rather than an all-or-nothing stop

**Files to modify:** `src/risk/adaptive_sizer.py` (new), `src/backtest/engine.py` (partial position closing)

**Expected impact:** Most promising for drawdown reduction without killing win rate. Instead of a hard stop at -5% (which creates losses), gradually reduce at -1%, -2%, -3%. Winners that recover keep some position; losers get small.

## Recommended Priority
1. **Approach C** (Adaptive Position Sizing) — easiest to implement, most likely to reduce DD without killing win rate
2. **Approach A** (Short Hedging) — more complex but direct DD cap
3. **Approach B** (Correlation Monitoring) — requires multi-asset data pipeline

## Key Files Reference
- `src/risk/` — All risk modules (regime_detector.py, pre_trade_filter.py, boltzmann_sizer.py, bootstrap_stops.py, drawdown_budget.py)
- `src/backtest/engine.py` — Main backtest loop, `_apply_risk_layer()`, `_get_trailing_params()`, `_update_positions_with_candle()`
- `src/strategies/momentum_strategy.py` — MomentumStrategy with Kelly sizing, Holt-Winters forecasting
- `src/ml/forecasting.py` — HoltWintersPredictor
- `scripts/backtest/zero_drawdown_comparison.py` — Comparison script for baseline vs risk layer
- `config/base.yaml` — All risk config params
- `tests/test_risk_smoke.py` — Smoke tests for all risk modules

## Key Lessons from Phase 1
1. **Bootstrap hard SL kills win rate** — removed. Trailing stops already handle losers well (80% WR)
2. **Regime detection is lagging** — CRASH is only detected AFTER the drop, not before
3. **MEAN_REVERTING is 89% of BTC market** — don't tighten trailing stops for it
4. **Boltzmann sizing is safe** — 51% reduction in MEAN_REVERTING only costs 19% of avg win
5. **The drawdown happens inside open positions** — pre-trade filters can't help; need intra-trade management
6. **Position partial closing is the missing piece** — the engine currently only supports all-or-nothing closes

## Venv
```bash
/home/dfr/pypsik/.venv/bin/python  # Python 3.12 with numpy, scipy, torch
```
