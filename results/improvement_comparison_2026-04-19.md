# Improvement Comparison Summary

**Date**: 2026-04-19
**Symbol**: BTCUSDT
**All backtests**: 7-day Bybit data (except where noted)

---

## 7-Day Results Summary

| Approach | Trades | Win Rate | Max DD | Sharpe | Return |
|----------|--------|----------|--------|--------|--------|
| **Baseline** | 14 | 35.7% | 227% | 4,447 | 0.02% |
| **+H1Model** | 6 | 50.0% | 7.76% | 17,230 | 0.02% |
| **+Trailing Stop (3.0/2.5)** | 6 | 50.0% | 7.76% | 18,296 | 0.02% |
| **+Kelly (10% cap)** | 6 | 50.0% | 35.1% | 22,636 | 0.08% |
| **+Meta-Labeling** | 6 | 50.0% | 7.76% | 17,408 | 0.02% |
| **+Regime Detection** | 506 | 16.2% | 200% | 996 | -0.0% |

---

## 30-Day Results

| Approach | Trades | Win Rate | Max DD | Sharpe | Return |
|----------|--------|----------|--------|--------|--------|
| **Baseline** | 33 | 33.3% | 305% | 1,246 | 0.05% |
| **Hybrid (all combined)** | 23 | 30.4% | 341% | 827 | 0.03% |

---

## Key Findings

### 1. H1Model Filtering ✅
- **Major improvement**: Win rate 35.7% → 50%, Drawdown 227% → 7.76%
- h1_model_filtered: 360 (7-day) / 1,716 (30-day)
- Best standalone improvement

### 2. Trailing Stop Optimization ✅
- Best config: activation=3.0 ATR, distance=2.5 ATR
- Marginal improvement over baseline (Sharpe 17,230 → 18,296)
- Low drawdown maintained (7.76%)

### 3. Kelly Criterion ⚠️
- Sharpe improved (17,230 → 22,636) but drawdown spiked (7.76% → 35.1%)
- 10% max position is too aggressive for this strategy
- **Recommendation**: Lower cap to 3-5% for safety

### 4. Meta-Labeling ⚠️
- Same win rate (50%) - not enough training data (3 trades only)
- Needs 30-day backtest to train properly
- Not enough signal data to learn good vs bad trades

### 5. Market Regime Detection ❌
- Regime switching destroyed performance
- Mean reversion not suitable as counter-strategy
- 506 trades with 16.2% win rate = bad strategy for this market

### 6. Hybrid (All Combined) ❌
- **Worse than baseline** on 30-day test
- Kelly's 10% cap drove DD from 305% → 341%
- Fewer trades (23 vs 33), lower win rate (30% vs 33%)
- **Conclusion**: Combining all approaches doesn't work; Kelly sizing is the problem

---

## Recommendations

### Current Best Configuration (7-day test):
```python
MomentumConfig(
    min_agreement=2,
    pullback_enabled=True,
    volume_spike_threshold=1.5,
    atr_filter_min_pct=0.0002,
    mtf_enabled=True,  # H1Model filtering
    kelly_sizing_enabled=False,  # Keep off for now
    trailing_activation_atr=3.0,
    trailing_distance_atr=2.5,
)
```

### Next Steps:
1. **Lower Kelly cap** to 3-5% if re-enabling Kelly
2. **Run 30-day with H1Model + best trailing stop only** (no Kelly)
3. **Train meta-labeling model on 30-day data** with more trades
4. **Consider ATR-based stops** instead of pure trailing stops

---

## Files Created

- `scripts/backtest/trailing_stop_optimization.py`
- `scripts/backtest/kelly_backtest.py`
- `src/ml/meta_label_model.py`
- `scripts/backtest/meta_label_backtest.py`
- `src/strategies/regime_aware_strategy.py`
- `scripts/backtest/regime_backtest.py`
- `scripts/backtest/h1_30day_backtest.py` (updated for hybrid)
- `scripts/backtest/hybrid_backtest.py` (hybrid config runner)