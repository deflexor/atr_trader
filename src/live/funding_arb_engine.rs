use crate::exchange::bybit::BybitClient;
use crate::strategy::funding_arb::{FundingArbConfig, FundingArbPosition};
use crate::data::funding::funding_rate_annualized;
use anyhow::{Context, Result};
use std::collections::HashMap;
use tokio::time::{Duration, timeout};
use tracing::{info, warn, error};

/// A live carry position tracked by the engine
#[derive(Debug, Clone)]
pub struct LiveCarryPosition {
    pub symbol: String,
    pub spot_qty: f64,         // base asset quantity (long)
    pub perp_qty: f64,         // contract qty (short)
    pub entry_funding_rate: f64,
    pub position_value_usd: f64,
    pub opened_at: i64,
    pub cumulative_funding: f64,
    pub entry_spot_price: f64,
    pub entry_perp_price: f64,
}

impl LiveCarryPosition {
    pub fn new(
        symbol: String,
        spot_qty: f64,
        perp_qty: f64,
        entry_funding_rate: f64,
        position_value_usd: f64,
        entry_spot_price: f64,
        entry_perp_price: f64,
    ) -> Self {
        Self {
            symbol,
            spot_qty,
            perp_qty,
            entry_funding_rate,
            position_value_usd,
            opened_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64,
            cumulative_funding: 0.0,
            entry_spot_price,
            entry_perp_price,
        }
    }
}

/// Live Funding Rate Arbitrage Engine
/// Runs an event loop checking funding rates and managing carry positions
pub struct FundingArbLiveEngine {
    pub bybit: BybitClient,
    pub config: FundingArbConfig,
    pub symbols: Vec<String>,
    pub capital: f64,
    pub positions: Vec<LiveCarryPosition>,
    pub dry_run: bool,
    pub total_funding_collected: f64,
    pub total_pnl: f64,
}

impl FundingArbLiveEngine {
    pub fn new(
        bybit: BybitClient,
        config: FundingArbConfig,
        symbols: Vec<String>,
        capital: f64,
        dry_run: bool,
    ) -> Self {
        Self {
            bybit,
            config,
            symbols,
            capital,
            positions: Vec::new(),
            dry_run,
            total_funding_collected: 0.0,
            total_pnl: 0.0,
        }
    }

    /// Run the main event loop forever
    pub async fn run(&mut self) -> Result<()> {
        info!("──────────────────────────────────────────────");
        info!("  Funding Rate Arb Live Engine");
        info!("──────────────────────────────────────────────");
        info!("  Exchange: bybit");
        info!("  Max positions: {}", self.config.max_positions);
        info!("  Rebalance: {} days", self.config.rebalance_days);
        info!("  Min rate: {:.6} ({:.1}% APR)", self.config.min_rate_threshold,
              funding_rate_annualized(self.config.min_rate_threshold));
        info!("  Capital: ${:.2}", self.capital);
        info!("  Dry run: {}", self.dry_run);
        info!("  Symbols: {}", self.symbols.join(", "));
        info!("──────────────────────────────────────────────");

        // Check API connectivity
        if !self.dry_run {
            match self.bybit.get_wallet_balance("UNIFIED").await {
                Ok(bal) => info!("  Bybit API connected — Balance: ${:.2}", bal.total_equity),
                Err(e) => warn!("  Balance fetch failed (keys may need setup): {e}"),
            }
        } else {
            info!("  Dry run mode — no orders placed");
        }

        // Run first check immediately, then every 8h
        loop {
            // Wrap in 60s timeout so the engine never hangs
            match timeout(Duration::from_secs(60), self.check_and_act()).await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => error!("  Check error: {e}"),
                Err(_) => error!("  Check timed out after 60s"),
            }
            info!("  Next check in 8 hours...");
            tokio::time::sleep(Duration::from_secs(8 * 3600)).await;
        }
    }

    /// Main check: fetch funding rates, compare to positions, open/close as needed
    async fn check_and_act(&mut self) -> Result<()> {
        info!("─── Funding check ───");

        // 1. Fetch current funding rates from Bybit
        let tickers = self.bybit.get_all_tickers().await
            .context("Failed to fetch tickers")?;

        let mut rates: Vec<(String, f64, f64)> = Vec::new(); // (symbol, rate, price)
        for sym in &self.symbols {
            // Bybit perp symbols are USDT-based. Convert USDC→USDT.
            let bybit_sym = if sym.ends_with("USDC") {
                format!("{}USDT", sym.trim_end_matches("USDC"))
            } else {
                sym.clone()
            };
            if let Some(t) = tickers.get(&bybit_sym) {
                rates.push((sym.clone(), t.funding_rate, t.last_price));
            } else if let Some(t) = tickers.get(sym) {
                rates.push((sym.clone(), t.funding_rate, t.last_price));
            }
        }

        if rates.is_empty() {
            warn!("  No funding rates found — check symbol format");
            return Ok(());
        }

        // Sort by rate descending
        rates.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

        // Log current rates
        info!("  Current rates:");
        for (sym, rate, price) in &rates {
            let apr = funding_rate_annualized(*rate);
            let status = if *rate >= self.config.min_rate_threshold { "✓" } else { "✗" };
            info!("    {status} {sym:12}: {:.6} per 8h ({:.1}% APR) @ ${:.2}", rate, apr, price);
        }

        let threshold = self.config.min_rate_threshold;
        let max_pos = self.config.max_positions;
        let rebalance_secs = self.config.rebalance_days as i64 * 86400;

        // 2. Check existing positions — close any below threshold or stale
        let mut to_close = Vec::new();
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;

        for pos in &self.positions {
            let current_rate = rates.iter()
                .find(|(s, _, _)| *s == pos.symbol)
                .map(|(_, r, _)| *r)
                .unwrap_or(0.0);

            let age = now - pos.opened_at;
            let should_close = current_rate < threshold || age >= rebalance_secs;

            if should_close {
                let reason = if current_rate < threshold {
                    format!("rate dropped to {:.6}", current_rate)
                } else {
                    format!("rebalance after {}d", age / 86400)
                };
                info!("  → Close {sym}: {reason}", sym = pos.symbol);
                to_close.push(pos.symbol.clone());
            }
        }

        // Close positions
        for sym in &to_close {
            if let Some(pos) = self.positions.iter().find(|p| p.symbol == *sym) {
                info!("  Closing carry: {} (spot {:.4} + perp {:.4})",
                    sym, pos.spot_qty, pos.perp_qty);
                if !self.dry_run {
                    self.close_position(sym).await?;
                }
                self.total_funding_collected += pos.cumulative_funding;
                info!("  ✓ {sym} closed — collected {:.6} in funding", pos.cumulative_funding);
            }
        }
        self.positions.retain(|p| !to_close.contains(&p.symbol));

        // 3. Open new positions from top of the list
        let available_slots = max_pos.saturating_sub(self.positions.len());
        if available_slots > 0 {
            let candidates: Vec<_> = rates.iter()
                .filter(|(sym, rate, _)| *rate >= threshold && !self.positions.iter().any(|p| &p.symbol == sym))
                .take(available_slots)
                .collect();

            for (sym, rate, price) in &candidates {
                // Calculate position size: equal weight
                let slot_value = self.capital / max_pos as f64;
                let qty = slot_value / price;
                // Round to sensible precision
                let qty = round_qty(qty);

                if qty < 0.001 {
                    warn!("  Skipping {sym}: qty too small ({:.6})", qty);
                    continue;
                }

                info!("  Opening carry: {sym} (spot long {:.4} + perp short {:.4}) @ ${:.2}, rate={:.6}",
                    qty, qty, price, rate);

                if !self.dry_run {
                    match self.open_position(sym, qty).await {
                        Ok(()) => {
                            self.positions.push(LiveCarryPosition::new(
                                sym.clone(), qty, qty, *rate, slot_value, *price, *price,
                            ));
                            info!("  ✓ {sym} position opened");
                        }
                        Err(e) => error!("  ✗ {sym} open failed: {e}"),
                    }
                } else {
                    self.positions.push(LiveCarryPosition::new(
                        sym.clone(), qty, qty, *rate, slot_value, *price, *price,
                    ));
                    info!("  ✓ {sym} (dry run — simulated)");
                }
            }
        }

        // 4. Collect funding on existing positions (tracked separately)
        // Funding is paid/received every 8h automatically. We track it by
        // checking position unrealised PnL change between checks.
        self.update_position_funding().await?;

        // 5. Summary
        info!("─── Portfolio Status ───");
        info!("  Open positions: {}", self.positions.len());
        let active_symbols: Vec<&str> = self.positions.iter().map(|p| p.symbol.as_str()).collect();
        info!("    {}", active_symbols.join(", "));
        info!("  Total funding collected: {:.6} USD", self.total_funding_collected);
        info!("  Total P&L: ${:.2}", self.total_pnl);

        Ok(())
    }

    /// Open a carry position: spot long + perp short
    async fn open_position(&self, symbol: &str, qty: f64) -> Result<()> {
        // 1. Set leverage to 1x on perp
        let _ = self.bybit.set_leverage(symbol, 1.0).await;

        // 2. Buy spot
        let spot = self.bybit.place_spot_order(symbol, "Buy", qty).await
            .with_context(|| format!("Spot buy failed for {symbol}"))?;
        info!("  Spot buy order: {}", spot.order_id);

        // 3. Short perp (same notional)
        let perp = self.bybit.place_perp_order(symbol, "Sell", qty).await
            .with_context(|| format!("Perp sell failed for {symbol}"))?;
        info!("  Perp sell order: {}", perp.order_id);

        Ok(())
    }

    /// Close a carry position: sell spot + buy back perp
    async fn close_position(&self, symbol: &str) -> Result<()> {
        // Get current position size from Bybit
        let positions = self.bybit.get_positions().await?;
        let perp_pos = positions.iter().find(|p| p.symbol == symbol);

        let perp_qty = perp_pos.map(|p| p.size).unwrap_or(0.0);

        // 1. Buy back perp (close short)
        if perp_qty > 0.0 {
            let buy = self.bybit.place_perp_order(symbol, "Buy", perp_qty).await?;
            info!("  Perp buy-to-close: {}", buy.order_id);
        }

        // 2. Sell spot (get spot qty from our tracking or use available balance)
        // For simplicity, sell the perp amount we just closed
        let spot_sell_qty = perp_qty;
        if spot_sell_qty > 0.0 {
            let sell = self.bybit.place_spot_order(symbol, "Sell", spot_sell_qty).await?;
            info!("  Spot sell: {}", sell.order_id);
        }

        Ok(())
    }

    /// Update cumulative funding from position P&L changes
    async fn update_position_funding(&mut self) -> Result<()> {
        if self.dry_run {
            // In dry run, simulate funding accrual
            for pos in &mut self.positions {
                pos.cumulative_funding += pos.position_value_usd * pos.entry_funding_rate.abs();
            }
            return Ok(());
        }

        // In live mode, we'd need to track per-8h P&L changes
        // For now, this is a placeholder that will be improved with DB tracking
        match self.bybit.get_positions().await {
            Ok(positions) => {
                for pos in &mut self.positions {
                    if let Some(p) = positions.iter().find(|p| p.symbol == pos.symbol) {
                        // Unrealised PnL from perp short = -price_change * qty + funding_income
                        // We can't split price PnL from funding without mark price history
                        // This will be refined with dedicated funding tracking
                        if p.unrealised_pnl > 0.0 {
                            pos.cumulative_funding += p.unrealised_pnl * 0.5; // rough approx
                        }
                    }
                }
            }
            Err(e) => warn!("  Failed to fetch positions for funding tracking: {e}"),
        }
        Ok(())
    }
}

fn round_qty(qty: f64) -> f64 {
    // Round to 4 significant decimal places
    let precision = 4;
    let factor = 10f64.powi(precision);
    (qty * factor).round() / factor
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_round_qty() {
        assert!((round_qty(1.234567) - 1.2346).abs() < 0.0001);
        assert!((round_qty(0.001234567) - 0.0012).abs() < 0.0001);
        assert!((round_qty(100.555) - 100.555).abs() < 0.001);
    }

    #[test]
    fn test_live_carry_position() {
        let pos = LiveCarryPosition::new(
            "BTCUSDT".into(), 0.1, 0.1, 0.0001, 10000.0, 50000.0, 50001.0,
        );
        assert_eq!(pos.symbol, "BTCUSDT");
        assert!((pos.spot_qty - 0.1).abs() < 0.001);
        assert!(pos.opened_at > 0);
    }
}
