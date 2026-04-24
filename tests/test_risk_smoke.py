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
    assert engine._adaptive_sizer is not None

    # Test with risk layer disabled
    cfg_off = BacktestConfig(use_zero_drawdown_layer=False)
    engine_off = BacktestEngine(config=cfg_off)
    assert engine_off._regime_detector is None
    assert engine_off._budget_tracker is None
    print("6. EngineRiskLayer: OK (on/off)")


def test_adaptive_sizer():
    """Test regime-aware adaptive position sizer."""
    from src.risk.adaptive_sizer import AdaptivePositionSizer, AdaptiveSizerConfig
    from src.risk.regime_detector import MarketRegime

    sizer = AdaptivePositionSizer()
    print("7. AdaptiveSizer (regime-aware):")

    # No reduction during CALM_TRENDING (not in active regimes)
    frac = sizer.evaluate(-5.0, 999, regime=MarketRegime.CALM_TRENDING, regime_energy=0.5)
    print(f"   -5.0% PnL, CALM_TRENDING: reduce={frac:.2f}")
    assert frac == 0.0

    # No reduction during MEAN_REVERTING (not in active regimes)
    frac = sizer.evaluate(-5.0, 999, regime=MarketRegime.MEAN_REVERTING, regime_energy=0.5)
    print(f"   -5.0% PnL, MEAN_REVERTING: reduce={frac:.2f}")
    assert frac == 0.0

    # CRASH: -2% → 25%
    frac = sizer.evaluate(-2.0, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   -2.0% PnL, CRASH: reduce={frac:.2f}")
    assert abs(frac - 0.25) < 0.01

    # CRASH: -3% → 50%
    frac = sizer.evaluate(-3.0, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   -3.0% PnL, CRASH: reduce={frac:.2f}")
    assert abs(frac - 0.50) < 0.01

    # CRASH: -5% → close entirely
    frac = sizer.evaluate(-5.0, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   -5.0% PnL, CRASH: reduce={frac:.2f}")
    assert abs(frac - 1.0) < 0.01

    # VOLATILE_TRENDING: -3% → 25%
    frac = sizer.evaluate(-3.0, 999, regime=MarketRegime.VOLATILE_TRENDING, regime_energy=0.5)
    print(f"   -3.0% PnL, VOLATILE_TRENDING: reduce={frac:.2f}")
    assert abs(frac - 0.25) < 0.01

    # No reduction when regime energy is too low
    frac = sizer.evaluate(-5.0, 999, regime=MarketRegime.CRASH, regime_energy=0.1)
    print(f"   -5.0% PnL, CRASH, energy=0.1: reduce={frac:.2f}")
    assert frac == 0.0

    # Cooldown: no reduction within cooldown window
    frac = sizer.evaluate(-5.0, 5, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   -5.0% PnL, CRASH, 5 candles since: reduce={frac:.2f}")
    assert frac == 0.0

    # No regime → no reduction
    frac = sizer.evaluate(-5.0, 999, regime=None, regime_energy=0.0)
    print(f"   -5.0% PnL, no regime: reduce={frac:.2f}")
    assert frac == 0.0


def test_position_reduce_entries():
    """Test Position.reduce_entries() for partial close support."""
    from src.core.models.position import Position

    pos = Position(symbol="BTC", side="long")
    pos.add_entry(70000.0, 0.01)
    pos.add_entry(71000.0, 0.01)
    pos.update_price(70000.0)

    print("8. Position.reduce_entries:")
    total_qty = pos.total_quantity
    print(f"   Initial: qty={total_qty:.4f}, entries={len(pos.entries)}")
    assert abs(total_qty - 0.02) < 1e-6

    # Reduce 50% — should remove first entry entirely
    closed_qty, closed_value = pos.reduce_entries(0.5)
    print(f"   After 50% reduce: closed_qty={closed_qty:.4f}, "
          f"closed_value={closed_value:.2f}, remaining_entries={len(pos.entries)}")
    assert abs(closed_qty - 0.01) < 1e-6
    assert abs(closed_value - 700.0) < 0.01  # 70000 * 0.01
    assert len(pos.entries) == 1
    assert abs(pos.total_quantity - 0.01) < 1e-6

    # Reduce 100% of remaining
    closed_qty, closed_value = pos.reduce_entries(1.0)
    print(f"   After 100% reduce: closed_qty={closed_qty:.4f}, entries={len(pos.entries)}")
    assert abs(closed_qty - 0.01) < 1e-6
    assert len(pos.entries) == 0


def test_velocity_tracker():
    """Test VelocityTracker rolling-window P&L velocity computation."""
    from src.risk.velocity_tracker import VelocityTracker, VelocityTrackerConfig, VelocityResult

    tracker = VelocityTracker(VelocityTrackerConfig(window_candles=5, min_samples=3))
    print("9. VelocityTracker:")

    # Simulate a position losing 0.5%/candle for 5 candles
    pos_id = 1
    pnl_series = [-0.1, -0.3, -0.6, -1.1, -1.6]
    for i, pnl in enumerate(pnl_series):
        tracker.update(pos_id, i, pnl)

    result = tracker.compute(pos_id)
    print(f"   Steady loss: velocity={result.velocity:.4f}/c, "
          f"acceleration={result.acceleration:.4f}/c², window={result.window_size}")
    assert result is not None
    assert result.window_size == 5
    assert result.velocity < 0  # Losing money
    assert abs(result.velocity - (-0.375)) < 0.01  # ~(-0.375 %/candle via regression)
    assert result.current_pnl_pct == -1.6

    # Not enough samples → None
    tracker2 = VelocityTracker(VelocityTrackerConfig(window_candles=5, min_samples=3))
    tracker2.update(99, 0, 0.0)
    tracker2.update(99, 1, -0.1)
    assert tracker2.compute(99) is None
    print("   Insufficient samples: correctly returns None")

    # Remove position data
    tracker.remove(pos_id)
    assert tracker.compute(pos_id) is None
    print("   Remove: correctly clears data")

    # Flat position → velocity ≈ 0
    tracker3 = VelocityTracker(VelocityTrackerConfig(window_candles=5, min_samples=3))
    for i in range(5):
        tracker3.update(42, i, -1.0)  # Constant -1%
    flat = tracker3.compute(42)
    print(f"   Flat P&L: velocity={flat.velocity:.4f}/c (should be ~0)")
    assert abs(flat.velocity) < 0.01

    # Reset clears all
    tracker3.reset()
    assert tracker3.tracked_positions == 0


def test_velocity_sizer():
    """Test VelocityPositionSizer regime-aware velocity thresholds."""
    from src.risk.velocity_sizer import VelocityPositionSizer, VelocitySizerConfig
    from src.risk.velocity_tracker import VelocityResult, VelocityTracker, VelocityTrackerConfig
    from src.risk.regime_detector import MarketRegime

    sizer = VelocityPositionSizer()
    print("10. VelocitySizer:")

    # CRASH: velocity -0.5%/c → 25% reduce
    result = VelocityResult(velocity=-0.6, acceleration=0.0, window_size=5, current_pnl_pct=-1.5)
    frac = sizer.evaluate(result, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   vel=-0.6/c, CRASH: reduce={frac:.2f}")
    assert abs(frac - 0.25) < 0.01

    # CRASH: velocity -1.0%/c → 50% reduce
    result = VelocityResult(velocity=-1.2, acceleration=0.0, window_size=5, current_pnl_pct=-2.0)
    frac = sizer.evaluate(result, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   vel=-1.2/c, CRASH: reduce={frac:.2f}")
    assert abs(frac - 0.50) < 0.01

    # CRASH: velocity -2.5%/c → close entirely
    result = VelocityResult(velocity=-2.5, acceleration=0.0, window_size=5, current_pnl_pct=-5.0)
    frac = sizer.evaluate(result, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   vel=-2.5/c, CRASH: reduce={frac:.2f}")
    assert abs(frac - 1.0) < 0.01

    # VOLATILE_TRENDING: velocity -0.3%/c → 25%
    result = VelocityResult(velocity=-0.4, acceleration=0.0, window_size=5, current_pnl_pct=-1.0)
    frac = sizer.evaluate(result, 999, regime=MarketRegime.VOLATILE_TRENDING, regime_energy=0.5)
    print(f"   vel=-0.4/c, VOLATILE_TRENDING: reduce={frac:.2f}")
    assert abs(frac - 0.25) < 0.01

    # CALM_TRENDING: no reduction (not in active regimes)
    result = VelocityResult(velocity=-1.0, acceleration=0.0, window_size=5, current_pnl_pct=-2.0)
    frac = sizer.evaluate(result, 999, regime=MarketRegime.CALM_TRENDING, regime_energy=0.5)
    print(f"   vel=-1.0/c, CALM_TRENDING: reduce={frac:.2f}")
    assert frac == 0.0

    # No reduction when P&L is positive (winning positions never cut)
    result = VelocityResult(velocity=-0.5, acceleration=0.0, window_size=5, current_pnl_pct=0.5)
    frac = sizer.evaluate(result, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   vel=-0.5/c, P&L=+0.5%: reduce={frac:.2f}")
    assert frac == 0.0

    # Cooldown: no reduction within cooldown window
    result = VelocityResult(velocity=-1.0, acceleration=0.0, window_size=5, current_pnl_pct=-2.0)
    frac = sizer.evaluate(result, 5, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   vel=-1.0/c, 5 candles since: reduce={frac:.2f}")
    assert frac == 0.0

    # No regime → no reduction
    result = VelocityResult(velocity=-1.0, acceleration=0.0, window_size=5, current_pnl_pct=-2.0)
    frac = sizer.evaluate(result, 999, regime=None, regime_energy=0.0)
    print(f"   vel=-1.0/c, no regime: reduce={frac:.2f}")
    assert frac == 0.0

    # Acceleration amplification: same velocity but accelerating loss → larger reduce
    result_no_accel = VelocityResult(velocity=-0.6, acceleration=0.0, window_size=5, current_pnl_pct=-1.5)
    result_accel = VelocityResult(velocity=-0.6, acceleration=-0.3, window_size=5, current_pnl_pct=-1.5)
    frac_no = sizer.evaluate(result_no_accel, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    frac_yes = sizer.evaluate(result_accel, 999, regime=MarketRegime.CRASH, regime_energy=0.5)
    print(f"   Acceleration: no_accel={frac_no:.3f}, with_accel={frac_yes:.3f}")
    assert frac_yes > frac_no  # Acceleration should amplify

    # Engine integration: velocity sizer initialized when use_velocity_sizing=True
    from src.backtest.engine import BacktestEngine, BacktestConfig
    cfg = BacktestConfig(use_zero_drawdown_layer=True, use_velocity_sizing=True)
    engine = BacktestEngine(config=cfg)
    assert engine._velocity_sizer is not None
    assert engine._velocity_tracker is not None
    print("   Engine integration: velocity sizer initialized OK")


def test_correlation_monitor():
    """Test CorrelationMonitor ETH/BTC divergence detection."""
    from src.risk.correlation_monitor import (
        CorrelationMonitor, CorrelationMonitorConfig, CorrelationRiskLevel,
    )
    print("11. CorrelationMonitor:")

    monitor = CorrelationMonitor(CorrelationMonitorConfig(lookback_candles=10, min_samples=3))

    # Feed normal correlated data — both rising together
    btc_base = 70000.0
    eth_base = 3500.0
    for i in range(12):
        btc_price = btc_base + i * 50  # BTC slowly rising
        eth_price = eth_base + i * 2   # ETH slowly rising
        monitor.update_btc(btc_price)
        monitor.update_eth(eth_price)

    signal = monitor.evaluate()
    print(f"   Normal: risk={signal.risk_level.value}, "
          f"eth_ret={signal.eth_return_pct:.2f}%, btc_ret={signal.btc_return_pct:.2f}%, "
          f"div={signal.divergence_pct:.2f}%")
    assert signal.risk_level == CorrelationRiskLevel.NORMAL
    assert signal.trailing_atr_multiplier is None  # No tightening needed

    # ETH drops 2% while BTC flat → ELEVATED (threshold: -0.8%)
    monitor2 = CorrelationMonitor(CorrelationMonitorConfig(lookback_candles=10, min_samples=3))
    for i in range(12):
        btc_price = btc_base  # BTC flat
        eth_price = eth_base * (1 - 0.002 * i)  # ETH slowly dropping
        monitor2.update_btc(btc_price)
        monitor2.update_eth(eth_price)

    signal = monitor2.evaluate()
    print(f"   ETH dropping, BTC flat: risk={signal.risk_level.value}, "
          f"eth_ret={signal.eth_return_pct:.2f}%, btc_ret={signal.btc_return_pct:.2f}%, "
          f"div={signal.divergence_pct:.2f}%")
    assert signal.risk_level in (CorrelationRiskLevel.ELEVATED, CorrelationRiskLevel.HIGH, CorrelationRiskLevel.EXTREME)
    assert signal.trailing_atr_multiplier is not None
    assert signal.trailing_atr_multiplier < 1.0  # Tighter trailing

    # ETH crashes 6% while BTC flat → at least HIGH (threshold: -1.6%)
    monitor3 = CorrelationMonitor(CorrelationMonitorConfig(lookback_candles=10, min_samples=3))
    for i in range(12):
        btc_price = btc_base  # BTC flat
        eth_price = eth_base * (1 - 0.006 * i)  # ETH crashing
        monitor3.update_btc(btc_price)
        monitor3.update_eth(eth_price)

    signal = monitor3.evaluate()
    print(f"   ETH crashing, BTC flat: risk={signal.risk_level.value}, "
          f"eth_ret={signal.eth_return_pct:.2f}%, btc_ret={signal.btc_return_pct:.2f}%, "
          f"div={signal.divergence_pct:.2f}%")
    assert signal.risk_level in (CorrelationRiskLevel.HIGH, CorrelationRiskLevel.EXTREME)
    assert signal.trailing_atr_multiplier is not None
    assert signal.trailing_atr_multiplier < 0.5  # Very tight
    assert signal.position_reduce_fraction > 0  # Reduce position

    # Both dropping together → NORMAL (no divergence, BTC already reacting)
    monitor4 = CorrelationMonitor(CorrelationMonitorConfig(lookback_candles=10, min_samples=3))
    for i in range(12):
        btc_price = btc_base * (1 - 0.005 * i)  # BTC also dropping
        eth_price = eth_base * (1 - 0.006 * i)  # ETH dropping slightly more
        monitor4.update_btc(btc_price)
        monitor4.update_eth(eth_price)

    signal = monitor4.evaluate()
    print(f"   Both dropping: risk={signal.risk_level.value}, "
          f"eth_ret={signal.eth_return_pct:.2f}%, btc_ret={signal.btc_return_pct:.2f}%, "
          f"div={signal.divergence_pct:.2f}%")
    # Should be NORMAL or ELEVATED — NOT extreme because BTC is already dropping
    assert signal.risk_level != CorrelationRiskLevel.EXTREME

    # Insufficient data → NORMAL
    monitor5 = CorrelationMonitor(CorrelationMonitorConfig(lookback_candles=10, min_samples=5))
    monitor5.update_btc(btc_base)
    monitor5.update_eth(eth_base)
    assert not monitor5.has_sufficient_data
    signal = monitor5.evaluate()
    assert signal.risk_level == CorrelationRiskLevel.NORMAL
    print("   Insufficient data: correctly returns NORMAL")

    # Reset clears all
    monitor3.reset()
    assert not monitor3.has_sufficient_data
    print("   Reset: correctly clears data")

    # ETH/BTC ratio tracking
    monitor6 = CorrelationMonitor(CorrelationMonitorConfig(lookback_candles=10, min_samples=3))
    for i in range(12):
        btc_price = btc_base + i * 100  # BTC rising
        eth_price = eth_base - i * 5    # ETH falling → ratio declining
        monitor6.update_btc(btc_price)
        monitor6.update_eth(eth_price)

    signal = monitor6.evaluate()
    print(f"   Ratio trend: eth_btc_ratio={signal.eth_btc_ratio:.6f}, "
          f"ratio_trend={signal.ratio_trend:.2f}%")
    assert signal.ratio_trend < 0  # Declining ratio

    # Engine integration: correlation monitor initialized when use_correlation_monitor=True
    from src.backtest.engine import BacktestEngine, BacktestConfig
    cfg = BacktestConfig(use_zero_drawdown_layer=True, use_correlation_monitor=True)
    engine = BacktestEngine(config=cfg)
    assert engine._correlation_monitor is not None
    print("   Engine integration: correlation monitor initialized OK")


if __name__ == "__main__":
    test_regime_detector()
    test_bootstrap_stops()
    test_drawdown_budget()
    test_boltzmann_sizer()
    test_pre_trade_filter()
    test_engine_risk_layer()
    test_adaptive_sizer()
    test_position_reduce_entries()
    test_velocity_tracker()
    test_velocity_sizer()
    test_correlation_monitor()
    print("\nAll smoke tests PASSED")
