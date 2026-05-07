use super::drawdown_budget::DrawdownBudgetTracker;
use super::regime_detector::MarketRegime;

/// Pre-trade filter verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TradeVerdict {
    Approved,
    RejectedBudget,
    RejectedRegime,
    RejectedWorstCase,
}

/// Immutable pre-trade evaluation result.
#[derive(Debug, Clone)]
pub struct TradeEvaluation {
    pub verdict: TradeVerdict,
    pub estimated_worst_loss: f64,
    pub estimated_drawdown_pct: f64,
    pub regime: MarketRegime,
    pub risk_multiplier: f64,
    pub reason: String,
}

/// Regime-specific risk multipliers for worst-case estimation.
fn regime_risk_multiplier(regime: MarketRegime) -> f64 {
    match regime {
        MarketRegime::CalmTrending => 1.5,
        MarketRegime::VolatileTrending => 3.0,
        MarketRegime::MeanReverting => 2.0,
        MarketRegime::Crash => f64::INFINITY,
    }
}

/// Evaluate whether a proposed trade should be executed.
pub fn evaluate_trade(
    budget_tracker: &DrawdownBudgetTracker,
    regime: MarketRegime,
    position_value: f64,
    capital: f64,
    atr_pct: f64,
) -> TradeEvaluation {
    let risk_mult = regime_risk_multiplier(regime);

    // CRASH regime: reject all
    if regime == MarketRegime::Crash {
        return TradeEvaluation {
            verdict: TradeVerdict::RejectedRegime,
            estimated_worst_loss: position_value,
            estimated_drawdown_pct: if capital > 0.0 {
                position_value / capital
            } else {
                1.0
            },
            regime,
            risk_multiplier: risk_mult,
            reason: "CRASH regime: all new entries blocked".to_string(),
        };
    }

    let worst_loss = position_value * atr_pct * risk_mult;
    let dd_pct = if capital > 0.0 {
        worst_loss / capital
    } else {
        1.0
    };

    // Check per-trade limit
    if dd_pct > budget_tracker.config.per_trade_budget_pct {
        return TradeEvaluation {
            verdict: TradeVerdict::RejectedWorstCase,
            estimated_worst_loss: worst_loss,
            estimated_drawdown_pct: dd_pct,
            regime,
            risk_multiplier: risk_mult,
            reason: format!(
                "Estimated DD {:.2}% exceeds per-trade limit {:.2}%",
                dd_pct * 100.0,
                budget_tracker.config.per_trade_budget_pct * 100.0
            ),
        };
    }

    // Check budget
    if !budget_tracker.can_enter_trade(worst_loss) {
        return TradeEvaluation {
            verdict: TradeVerdict::RejectedBudget,
            estimated_worst_loss: worst_loss,
            estimated_drawdown_pct: dd_pct,
            regime,
            risk_multiplier: risk_mult,
            reason: "Drawdown budget insufficient for this trade".to_string(),
        };
    }

    TradeEvaluation {
        verdict: TradeVerdict::Approved,
        estimated_worst_loss: worst_loss,
        estimated_drawdown_pct: dd_pct,
        regime,
        risk_multiplier: risk_mult,
        reason: String::new(),
    }
}
