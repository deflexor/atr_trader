//! DEX fill simulation with price impact and gas costs.
//!
//! Unlike CEX order-book commissions, DEX trades incur:
//! - Price impact: function of trade size relative to pool depth
//! - Gas: fixed per-transaction cost (Solana ~0.000005 SOL)
//!
//! For backtesting, we use a simplified model:
//! - Price impact = slippage_bps / 10000, direction-adjusted
//! - Gas = fixed SOL amount, converted to quote at current price

/// DEX fill simulator.
#[derive(Debug, Clone)]
pub struct FillSimulator {
    /// Slippage in basis points (1 bps = 0.01%)
    pub slippage_bps: u64,
    /// Fixed gas cost in SOL
    pub gas_cost_sol: f64,
    /// SOL price in quote currency (for gas cost conversion)
    pub sol_price: f64,
}

impl FillSimulator {
    pub fn new(slippage_bps: u64, gas_cost_sol: f64, sol_price: f64) -> Self {
        Self {
            slippage_bps,
            gas_cost_sol,
            sol_price,
        }
    }

    /// Calculate the effective fill price for a trade.
    ///
    /// For buys (is_buy=true): price increases by slippage (you buy at a premium).
    /// For sells (is_buy=false): price decreases by slippage (you sell at a discount).
    pub fn fill_price(&self, quote_price: f64, is_buy: bool) -> f64 {
        let direction_mult = if is_buy { 1.0 } else { -1.0 };
        let impact = (self.slippage_bps as f64 / 10_000.0) * direction_mult;
        quote_price * (1.0 + impact)
    }

    /// Calculate gas cost in quote currency.
    pub fn gas_cost_quote(&self) -> f64 {
        self.gas_cost_sol * self.sol_price
    }
}

impl Default for FillSimulator {
    fn default() -> Self {
        Self {
            slippage_bps: 50,   // 0.5%
            gas_cost_sol: 0.000_005,
            sol_price: 150.0,   // Approximate SOL price
        }
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fill_buy_premium() {
        let sim = FillSimulator::new(50, 0.000_005, 150.0);
        let price = sim.fill_price(100.0, true);
        // 100 * (1 + 0.005) = 100.50
        assert!((price - 100.50).abs() < 0.001, "Expected 100.50, got {price}");
    }

    #[test]
    fn test_fill_sell_discount() {
        let sim = FillSimulator::new(50, 0.000_005, 150.0);
        let price = sim.fill_price(100.0, false);
        // 100 * (1 - 0.005) = 99.50
        assert!((price - 99.50).abs() < 0.001, "Expected 99.50, got {price}");
    }

    #[test]
    fn test_no_slippage() {
        let sim = FillSimulator::new(0, 0.000_005, 150.0);
        let price = sim.fill_price(100.0, true);
        assert!((price - 100.0).abs() < 0.001);
    }

    #[test]
    fn test_gas_cost() {
        let sim = FillSimulator::new(50, 0.000_005, 150.0);
        let gas = sim.gas_cost_quote();
        assert!((gas - 0.00075).abs() < 0.000_001, "Expected 0.00075, got {gas}");
    }
}
