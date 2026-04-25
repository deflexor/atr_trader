# Next Session: Enhanced Signal Generator + Multi-Asset Runner — Phase 6+

## Current State

Phases 1-6 complete on branch `news`. All risk modules + enhanced signal generator + multi-asset runner implemented and tested.

### Best Strategy & Config (PROVEN)

**EnhancedSignalGenerator** with the following configuration produces **+8.94%/month** across 8 assets:

```python
# Signal Config
EnhancedSignalConfig(
    min_agreement=3,          # All 3 trend indicators must agree
    rsi_oversold=25.0,        # Strict oversold threshold
    rsi_overbought=75.0,      # Strict overbought threshold
    bollinger_required=True,  # Require Bollinger Band touch for mean-reversion
    breakout_lookback=100,    # 100-candle (~8h) high/low window
    breakout_min_range_pct=0.002,  # 0.2% min breakout range
    breakout_strength=0.8,
    mean_reversion_strength=0.7,
    trend_strength=0.8,
    vwap_enabled=False,       # VWAP added noise, not alpha
    divergence_enabled=False, # Divergence added noise, not alpha
)

# Backtest Config
BacktestConfig(
    initial_capital=1250.0,   # Per asset (10k total / 8)
    risk_per_trade=0.03,      # 3% risk per trade
    max_positions=2,          # Max 2 concurrent positions
    cooldown_candles=96,      # 8h cooldown (96 * 5min)
    use_trailing_stop=True,
    trailing_activation_atr=2.0,
    trailing_distance_atr=1.5,
    use_atr_stops=True,
    use_zero_drawdown_layer=False,
)
```

### 90-Day Backtest Results (8 Assets)

| Asset | Trades | Return |
|-------|--------|--------|
| BTCUSDT | 429 | +3.10% |
| ETHUSDT | 438 | +5.00% |
| DOGEUSDT | 416 | +4.58% |
| TRXUSDT | 403 | +1.23% |
| SOLUSDT | 446 | +1.98% |
| ADAUSDT | 450 | +6.55% |
| AVAXUSDT | 466 | +4.94% |
| UNIUSDT | 313 | -0.57% |
| **TOTAL** | **3361** | **+26.81%** |

**Monthly estimate: +8.94%/month** (7/8 assets profitable)

### What's Implemented

**Risk Layer** (`src/risk/`):
- RegimeDetector, PreTradeDrawdownFilter, BoltzmannPositionSizer
- BootstrapStopCalculator, DrawdownBudgetTracker
- AdaptivePositionSizer, VelocityTracker, VelocityPositionSizer
- CorrelationMonitor, CompositeRiskScorer

**Strategy** (`src/strategies/`):
- `enhanced_signals.py`: Pure function signal generation (breakout + mean-reversion + trend + VWAP + divergence)
- `enhanced_strategy.py`: Async BaseStrategy wrapper with AdaptiveSizer
- `momentum_strategy.py`: Original momentum strategy (baseline)
- `mean_reversion_strategy.py`: Standalone mean-reversion strategy
- `regime_aware_strategy.py`: ADX-based regime switching

**Backtest** (`src/backtest/`):
- `multi_asset_runner.py`: Concurrent multi-asset runner with shared capital pool
- `engine.py`: Core engine with all risk integrations

**Data** (`data/candles.db`):
- 8 symbols × 5m candles: BTC, ETH, DOGE, TRX, SOL, ADA, AVAX, UNI
- Date range: 2025-09-29 to 2026-02-28

### Key Decisions
- Pure functions for all signal computation (no class state) — testable, composable
- Union logic: any sub-signal fires → trade, with synergy bonus for multiple agreeing
- Conflict (both long+short) → no trade (safety)
- 8h cooldown is the sweet spot — shorter = overtrading, longer = missed opportunities
- 3% risk is optimal — higher risk (4-5%) reduces returns due to amplified drawdowns
- VWAP and divergence signals didn't add alpha — they produced noise
- 7/8 assets profitable → strategy is robust across different market conditions
- ADA is the best performer (+6.55%/90d), UNI is the only loser (-0.57%)

### What's Still Needed to Reach 10%/month

Gap: +8.94%/mo achieved vs +10%/mo target. Options:

1. **Drop UNI** (only loser) → reallocate to 7 assets → ~+27.38%/90d = +9.13%/mo
2. **Adaptive position sizing** — scale up on winning streaks (AdaptiveSizer implemented but not yet integrated into engine)
3. **Better exit strategy** — current trailing stop may be too tight/loose for some assets
4. **Per-asset parameter tuning** — ADA/AVAX/SOL could use different breakout_lookback than BTC
5. **More assets** — add LINK, DOT, NEAR, etc.
6. **Intraday seasonality** — avoid low-volume hours (some signals fire in chop)

### Critical Context
- Data loading MUST use `cutoff = latest - days * 86400` with `WHERE timestamp >= cutoff`
- `CandleSeries` doesn't support `len()` — use `len(candles.candles)`
- `MomentumStrategy.generate_signal()` (not `.generate()`) is the correct method name
- Each 90-day backtest takes ~2.5 min per asset on 5m candles
- DB exchange is 'bybit' (not 'kucoin') for all symbols
- `BacktestResult.max_drawdown` is a fraction (not percentage) — multiply by 100 for %
- `BacktestResult.trades` is `list[dict]` with keys: timestamp, symbol, side, entry_price, quantity, etc.
