/// Drawdown budget tracker — allocates per-session drawdown budget and tracks consumption.

/// Immutable configuration for drawdown budget tracking.
#[derive(Debug, Clone)]
pub struct DrawdownBudgetConfig {
    pub total_budget_pct: f64,
    pub per_trade_budget_pct: f64,
    pub recovery_threshold_pct: f64,
    pub cooldown_after_halt: i32,
}

impl Default for DrawdownBudgetConfig {
    fn default() -> Self {
        Self {
            total_budget_pct: 0.03,
            per_trade_budget_pct: 0.01,
            recovery_threshold_pct: 0.5,
            cooldown_after_halt: 10,
        }
    }
}

/// Tracks cumulative drawdown budget consumption.
pub struct DrawdownBudgetTracker {
    pub config: DrawdownBudgetConfig,
    pub initial_capital: f64,
    pub peak_equity: f64,
    pub current_equity: f64,
    pub consumed_budget: f64,
    pub is_halted: bool,
    pub halt_candle: i32,
    pub candles_since_halt: i32,
}

impl DrawdownBudgetTracker {
    pub fn new(config: DrawdownBudgetConfig, initial_capital: f64) -> Self {
        Self {
            config,
            initial_capital,
            peak_equity: initial_capital,
            current_equity: initial_capital,
            consumed_budget: 0.0,
            is_halted: false,
            halt_candle: -999,
            candles_since_halt: 0,
        }
    }

    pub fn total_budget(&self) -> f64 {
        self.initial_capital * self.config.total_budget_pct
    }

    pub fn per_trade_budget(&self) -> f64 {
        self.initial_capital * self.config.per_trade_budget_pct
    }

    pub fn budget_remaining(&self) -> f64 {
        (self.total_budget() - self.consumed_budget).max(0.0)
    }

    /// Update equity tracking and check halt/resume conditions.
    pub fn update_equity(&mut self, equity: f64, candle_index: i32) {
        self.current_equity = equity;
        self.peak_equity = self.peak_equity.max(equity);

        if self.peak_equity > 0.0 {
            let drawdown = self.peak_equity - equity;
            self.consumed_budget = self.consumed_budget.max(drawdown);
        }

        // Check halt
        if self.consumed_budget >= self.total_budget() && !self.is_halted {
            self.is_halted = true;
            self.halt_candle = candle_index;
        }

        // Check resume
        if self.is_halted {
            self.candles_since_halt = candle_index - self.halt_candle;
            let recovery_level = self.total_budget() * self.config.recovery_threshold_pct;
            if self.consumed_budget <= recovery_level
                && self.candles_since_halt >= self.config.cooldown_after_halt
            {
                self.is_halted = false;
            }
        }
    }

    /// Check if a new trade is allowed given budget constraints.
    pub fn can_enter_trade(&self, estimated_loss: f64) -> bool {
        if self.is_halted {
            return false;
        }
        if estimated_loss > 0.0 && estimated_loss > self.budget_remaining() {
            return false;
        }
        if estimated_loss > self.per_trade_budget() {
            return false;
        }
        true
    }
}
