"""Smoke test for zero-drawdown risk layer modules."""
import numpy as np


def test_regime_detector():
    from src.risk.regime_detector import RegimeDetector, MarketRegime

    detector = RegimeDetector(lookback=50, min_samples=20)
    np.random.seed(42)
    calm = np.random.normal(0.001, 0.005, 100).tolist()
    for r in calm:
        detector.update(r)
    result = detector.detect()
    print(f"1. RegimeDetector (calm): regime={result.regime.value}, "
          f"conf={result.confidence:.2f}, energy={result.energy:.2f}")
    assert result.regime in [MarketRegime.CALM_TRENDING, MarketRegime.MEAN_REVERTING]

    detector.reset()
    crash = np.concatenate([np.random.normal(-0.01, 0.03, 70),
                            np.random.normal(-0.05, 0.08, 30)]).tolist()
    for r in crash:
        detector.update(r)
    result = detector.detect()
    print(f"   RegimeDetector (crash): regime={result.regime.value}, "
          f"conf={result.confidence:.2f}, energy={result.energy:.2f}")
    assert result.regime == MarketRegime.CRASH


def test_bootstrap_stops():
    from src.risk.bootstrap_stops import compute_bootstrap_stop

    returns = np.random.normal(0.0, 0.02, 200).tolist()
    result = compute_bootstrap_stop(returns, confidence_level=0.95,
                                   n_simulations=500, seed=42)
    print(f"2. BootstrapStops: stop={result.stop_distance_pct:.4f} "
          f"({result.stop_distance_pct*100:.2f}%), worst={result.worst_case_pct:.4f}")
    assert 0.003 <= result.stop_distance_pct <= 0.05


def test_drawdown_budget():
    from src.risk.drawdown_budget import DrawdownBudgetTracker, DrawdownBudgetConfig

    budget = DrawdownBudgetTracker(
        config=DrawdownBudgetConfig(total_budget_pct=0.03, per_trade_budget_pct=0.01),
        initial_capital=10000.0,
    )
    print(f"3. BudgetTracker: total={budget.total_budget:.0f}, per_trade={budget.per_trade_budget:.0f}")
    assert budget.can_enter_trade(50) is True
    assert budget.can_enter_trade(200) is False  # exceeds per-trade

    budget.update_equity(9800, 5)
    print(f"   After 2% DD: consumed={budget.consumed_budget:.0f}, halted={budget.is_halted}")
    budget.update_equity(9600, 10)
    print(f"   After 4% DD: consumed={budget.consumed_budget:.0f}, halted={budget.is_halted}")
    assert budget.is_halted is True


def test_boltzmann_sizer():
    from src.risk.boltzmann_sizer import BoltzmannPositionSizer, BoltzmannConfig
    from src.risk.regime_detector import MarketRegime, RegimeResult

    sizer = BoltzmannPositionSizer(BoltzmannConfig(temperature=0.3))
    print("4. BoltzmannSizer:")
    for regime in MarketRegime:
        # Use realistic energy: low for calm, high for crash
        energy_map = {
            MarketRegime.CALM_TRENDING: 0.12,
            MarketRegime.VOLATILE_TRENDING: 0.44,
            MarketRegime.MEAN_REVERTING: 0.50,
            MarketRegime.CRASH: 0.90,
        }
        rr = RegimeResult(regime=regime, confidence=0.8, volatility_percentile=0.5,
                          skewness=0.0, kurtosis_val=0.0, energy=energy_map[regime])
        frac = sizer.calculate_size_fraction(rr)
        print(f"   {regime.value:20s}: fraction={frac:.3f}")
    # CRASH should give 0.0
    crash_rr = RegimeResult(regime=MarketRegime.CRASH, confidence=0.9,
                            volatility_percentile=0.9, skewness=-3.0,
                            kurtosis_val=5.0, energy=0.9)
    crash_frac = sizer.calculate_size_fraction(crash_rr)
    assert crash_frac == 0.0 or crash_frac <= 0.05


def test_pre_trade_filter():
    from src.risk.pre_trade_filter import PreTradeDrawdownFilter, TradeVerdict
    from src.risk.drawdown_budget import DrawdownBudgetTracker
    from src.risk.regime_detector import MarketRegime, RegimeResult

    budget = DrawdownBudgetTracker(initial_capital=10000.0)
    filt = PreTradeDrawdownFilter(budget, max_per_trade_dd_pct=0.01)

    calm_result = RegimeResult(regime=MarketRegime.CALM_TRENDING, confidence=0.9,
                               volatility_percentile=0.3, skewness=0.1,
                               kurtosis_val=0.0, energy=0.1)
    eval_calm = filt.evaluate(calm_result, position_value=100, capital=10000, atr_pct=0.01)
    print(f"5. PreTradeFilter (calm): verdict={eval_calm.verdict.value}")
    assert eval_calm.verdict == TradeVerdict.APPROVED

    crash_result = RegimeResult(regime=MarketRegime.CRASH, confidence=0.95,
                                volatility_percentile=0.9, skewness=-3.0,
                                kurtosis_val=5.0, energy=0.9)
    eval_crash = filt.evaluate(crash_result, position_value=100, capital=10000, atr_pct=0.01)
    print(f"   PreTradeFilter (crash): verdict={eval_crash.verdict.value}")
    assert eval_crash.verdict == TradeVerdict.REJECTED_REGIME


def test_engine_risk_layer():
    """Test that the engine initializes with risk layer and filters correctly."""
    from src.backtest.engine import BacktestEngine, BacktestConfig

    cfg = BacktestConfig(use_zero_drawdown_layer=True)
    engine = BacktestEngine(config=cfg)

    assert engine._regime_detector is not None
    assert engine._budget_tracker is not None
    assert engine._boltzmann_sizer is not None
    assert engine._bootstrap_stops is not None
    assert engine._pre_trade_filter is not None

    # Test with risk layer disabled
    cfg_off = BacktestConfig(use_zero_drawdown_layer=False)
    engine_off = BacktestEngine(config=cfg_off)
    assert engine_off._regime_detector is None
    assert engine_off._budget_tracker is None
    print("6. EngineRiskLayer: OK (on/off)")


if __name__ == "__main__":
    test_regime_detector()
    test_bootstrap_stops()
    test_drawdown_budget()
    test_boltzmann_sizer()
    test_pre_trade_filter()
    test_engine_risk_layer()
    print("\nAll smoke tests PASSED")
