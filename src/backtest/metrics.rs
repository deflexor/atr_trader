//! Performance metrics for backtest results.
//!
//! Pure functions where possible. Computes Sharpe ratio,
//! max drawdown, win rate, and other statistics.

/// Aggregate performance metrics.
#[derive(Debug, Clone)]
pub struct PerformanceMetrics {
    pub total_return_pct: f64,
    pub max_drawdown_pct: f64,
    pub max_drawdown_vs_initial: f64,
    pub sharpe_ratio: f64,
    pub win_rate: f64,
    pub total_trades: usize,
    pub winning_trades: usize,
    pub losing_trades: usize,
    pub avg_win: f64,
    pub avg_loss: f64,
    pub profit_factor: f64,
}

/// Compute Sharpe ratio from periodic returns. Pure function.
///
/// Uses risk-free rate = 0 (standard for crypto backtests).
/// Annualized = daily_sharpe * sqrt(365).
pub fn compute_sharpe(daily_returns: &[f64]) -> f64 {
    if daily_returns.len() < 2 {
        return 0.0;
    }
    let n = daily_returns.len() as f64;
    let mean = daily_returns.iter().sum::<f64>() / n;
    let variance = daily_returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let std = variance.sqrt();
    if std == 0.0 {
        return 0.0;
    }
    let daily_sharpe = mean / std;
    // Annualize: trading days per year ≈ 365 for crypto
    daily_sharpe * 365.0_f64.sqrt()
}

/// Compute maximum drawdown from equity curve. Pure function.
///
/// Returns the maximum peak-to-trough drawdown as a fraction (0.0-1.0).
pub fn compute_max_drawdown(equity: &[f64]) -> f64 {
    if equity.len() < 2 {
        return 0.0;
    }
    let mut peak = equity[0];
    let mut max_dd = 0.0;
    for &value in equity.iter().skip(1) {
        if value > peak {
            peak = value;
        } else {
            let dd = (peak - value) / peak;
            if dd > max_dd {
                max_dd = dd;
            }
        }
    }
    max_dd
}

/// Compute maximum drawdown from initial capital. Pure function.
pub fn compute_max_drawdown_vs_initial(equity: &[f64], initial: f64) -> f64 {
    if initial <= 0.0 {
        return 0.0;
    }
    equity
        .iter()
        .map(|&e| (initial - e).max(0.0) / initial)
        .fold(0.0_f64, f64::max)
}

/// Compute win rate from trade P&Ls. Pure function.
pub fn compute_win_rate(pnls: &[f64]) -> f64 {
    if pnls.is_empty() {
        return 0.0;
    }
    let wins = pnls.iter().filter(|&&p| p > 0.0).count();
    wins as f64 / pnls.len() as f64
}

/// Compute profit factor (gross wins / gross losses). Pure function.
pub fn compute_profit_factor(pnls: &[f64]) -> f64 {
    let gross_wins: f64 = pnls.iter().filter(|&&p| p > 0.0).sum();
    let gross_losses: f64 = pnls.iter().filter(|&&p| p < 0.0).sum::<f64>().abs();
    if gross_losses == 0.0 {
        return if gross_wins > 0.0 { f64::INFINITY } else { 0.0 };
    }
    gross_wins / gross_losses
}

/// Compute daily returns from equity curve. Pure function.
pub fn compute_daily_returns(equity: &[f64]) -> Vec<f64> {
    if equity.len() < 2 {
        return Vec::new();
    }
    equity
        .windows(2)
        .map(|w| (w[1] - w[0]) / w[0])
        .collect()
}

/// Compute all metrics from trade records and equity curve.
pub fn compute_metrics(equity_curve: &[f64], closed_pnls: &[f64], initial_capital: f64) -> PerformanceMetrics {
    let total_return_pct = if initial_capital > 0.0 {
        let final_eq = equity_curve.last().copied().unwrap_or(initial_capital);
        ((final_eq - initial_capital) / initial_capital) * 100.0
    } else {
        0.0
    };

    let max_drawdown_pct = compute_max_drawdown(equity_curve) * 100.0;
    let max_drawdown_vs_initial = compute_max_drawdown_vs_initial(equity_curve, initial_capital) * 100.0;

    let daily_returns = compute_daily_returns(equity_curve);
    let sharpe_ratio = compute_sharpe(&daily_returns);

    let total_trades = closed_pnls.len();
    let winning_trades = closed_pnls.iter().filter(|&&p| p > 0.0).count();
    let losing_trades = closed_pnls.iter().filter(|&&p| p <= 0.0).count();
    let win_rate = compute_win_rate(closed_pnls) * 100.0;

    let winning_pnls: Vec<f64> = closed_pnls.iter().filter(|&&p| p > 0.0).copied().collect();
    let losing_pnls: Vec<f64> = closed_pnls.iter().filter(|&&p| p <= 0.0).copied().collect();

    let avg_win = if winning_trades > 0 {
        winning_pnls.iter().sum::<f64>() / winning_trades as f64
    } else {
        0.0
    };

    let avg_loss = if losing_trades > 0 {
        losing_pnls.iter().sum::<f64>() / losing_trades as f64
    } else {
        0.0
    };

    let profit_factor = compute_profit_factor(closed_pnls);

    PerformanceMetrics {
        total_return_pct,
        max_drawdown_pct,
        max_drawdown_vs_initial,
        sharpe_ratio,
        win_rate,
        total_trades,
        winning_trades,
        losing_trades,
        avg_win,
        avg_loss,
        profit_factor,
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sharpe_constant_returns() {
        // Constant 1% daily return → infinite Sharpe (zero variance)
        let returns = vec![0.01, 0.01, 0.01, 0.01, 0.01];
        let sharpe = compute_sharpe(&returns);
        // Should be 0.0 (variance = 0)
        assert!((sharpe - 0.0).abs() < 0.001);
    }

    #[test]
    fn test_sharpe_positive() {
        let returns = vec![0.02, -0.01, 0.015, 0.005, -0.005, 0.01];
        let sharpe = compute_sharpe(&returns);
        assert!(sharpe > 0.0, "Sharpe should be positive for mostly positive returns");
    }

    #[test]
    fn test_max_drawdown() {
        let equity = vec![100.0, 110.0, 90.0, 95.0, 85.0, 100.0];
        let dd = compute_max_drawdown(&equity);
        // Peak = 110, trough = 85 → DD = (110-85)/110 ≈ 0.2273
        assert!((dd - 0.2273).abs() < 0.001, "Expected ~0.227, got {dd}");
    }

    #[test]
    fn test_max_drawdown_monotonic() {
        let equity = vec![100.0, 105.0, 110.0, 115.0];
        let dd = compute_max_drawdown(&equity);
        assert!((dd - 0.0).abs() < 0.001, "No drawdown expected");
    }

    #[test]
    fn test_win_rate() {
        let pnls = vec![100.0, -50.0, 200.0, -30.0, 50.0];
        let wr = compute_win_rate(&pnls);
        assert!((wr - 0.6).abs() < 0.001, "Expected 60% win rate, got {wr}");
    }

    #[test]
    fn test_win_rate_empty() {
        assert!((compute_win_rate(&[]) - 0.0).abs() < 0.001);
    }

    #[test]
    fn test_profit_factor() {
        let pnls = vec![100.0, -50.0, 200.0, -30.0];
        let pf = compute_profit_factor(&pnls);
        // 300 / 80 = 3.75
        assert!((pf - 3.75).abs() < 0.001, "Expected 3.75, got {pf}");
    }

    #[test]
    fn test_profit_factor_no_losses() {
        assert!(compute_profit_factor(&[100.0, 50.0]).is_infinite());
    }

    #[test]
    fn test_compute_metrics() {
        let equity = vec![10000.0, 10100.0, 10200.0, 10150.0, 10300.0];
        let pnls = vec![150.0, -50.0, 200.0];
        let metrics = compute_metrics(&equity, &pnls, 10000.0);
        assert!((metrics.total_return_pct - 3.0).abs() < 0.1, "Return ~3%");
        assert!((metrics.win_rate - 66.67).abs() < 1.0, "Win rate ~66.7%");
        assert_eq!(metrics.total_trades, 3);
        assert!(metrics.avg_win > 0.0);
        assert!(metrics.avg_loss < 0.0);
    }

    #[test]
    fn test_daily_returns() {
        let equity = vec![100.0, 105.0, 110.0];
        let returns = compute_daily_returns(&equity);
        assert_eq!(returns.len(), 2);
        assert!((returns[0] - 0.05).abs() < 0.001, "Expected 5%, got {}", returns[0]);
        assert!((returns[1] - 0.0476).abs() < 0.001, "Expected ~4.76%, got {}", returns[1]);
    }

    #[test]
    fn test_daily_returns_insufficient() {
        assert!(compute_daily_returns(&[100.0]).is_empty());
    }
}
