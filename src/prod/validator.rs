//! Config validation — checks required fields and environment.
//!
//! Run at startup to catch misconfiguration early.
//! Returns a list of warnings (non-fatal) and errors (fatal).

use std::path::Path;

use crate::config::Config;

/// A validation message.
#[derive(Debug, Clone)]
pub enum ValidationMessage {
    Error(String),
    Warning(String),
}

impl ValidationMessage {
    pub fn is_error(&self) -> bool {
        matches!(self, ValidationMessage::Error(_))
    }
}

/// Validate the entire config, returning any issues found.
pub fn validate_config(config: &Config, data_dir: &Path) -> Vec<ValidationMessage> {
    let mut messages = Vec::new();

    // Data directory
    if !data_dir.exists() {
        messages.push(ValidationMessage::Error(format!(
            "Data directory does not exist: {}",
            data_dir.display()
        )));
    }

    // Market data
    if config.market_data.symbols.is_empty() {
        messages.push(ValidationMessage::Warning(
            "No market symbols configured. Trading will be inactive.".into(),
        ));
    }

    if config.market_data.timeframe.is_empty() {
        messages.push(ValidationMessage::Error(
            "Market data timeframe is required (e.g. '5m', '1h').".into(),
        ));
    }

    // News
    if config.news.cryptopanic_api_key.is_none() && config.news.sources.contains(&"cryptopanic".into()) {
        messages.push(ValidationMessage::Warning(
            "CryptoPanic API key not configured. News sentiment will be unavailable. \
             Set ATR_CRYPTOPANIC_KEY or configure cryptopanic_api_key in config.toml."
                .into(),
        ));
    }

    // Strategy
    if config.strategy.risk_per_trade <= 0.0 || config.strategy.risk_per_trade > 1.0 {
        messages.push(ValidationMessage::Error(
            format!(
                "risk_per_trade must be between 0.0 and 1.0, got {}",
                config.strategy.risk_per_trade
            )
        ));
    }

    if config.strategy.max_positions == 0 {
        messages.push(ValidationMessage::Warning(
            "max_positions is 0 — no positions will be opened.".into(),
        ));
    }

    // Signals
    if config.strategy.enabled_signals.is_empty() {
        messages.push(ValidationMessage::Warning(
            "No signals enabled. All signals will be NEUTRAL.".into(),
        ));
    }

    // DEX
    if config.dex.dex != "jupiter" {
        messages.push(ValidationMessage::Warning(format!(
            "Unknown DEX '{}'. Only 'jupiter' is currently supported.",
            config.dex.dex
        )));
    }

    if config.dex.rpc_url.is_empty() {
        messages.push(ValidationMessage::Error(
            "Solana RPC URL is required for live trading.".into(),
        ));
    }

    // Live trading
    if config.live.enabled {
        if config.dex.wallet_private_key.is_none() {
            messages.push(ValidationMessage::Warning(
                "Live trading enabled but no wallet key configured. \
                 Set ATR_WALLET_KEY or configure wallet_private_key."
                    .into(),
            ));
        }

        if config.live.symbols.is_empty() {
            messages.push(ValidationMessage::Warning(
                "Live trading enabled but no symbols configured.".into(),
            ));
        }

        if config.live.max_drawdown_pct <= 0.0 || config.live.max_drawdown_pct > 1.0 {
            messages.push(ValidationMessage::Warning(
                format!(
                    "max_drawdown_pct should be between 0.01 and 1.0 (representing 1%-100%), got {}",
                    config.live.max_drawdown_pct
                )
            ));
        }
    }

    // Risk
    if config.risk.total_drawdown_budget <= 0.0 {
        messages.push(ValidationMessage::Warning(
            "total_drawdown_budget is 0 or negative — drawdown protection disabled.".into(),
        ));
    }

    messages
}

/// Print validation messages and return true if any errors exist.
pub fn print_validation(messages: &[ValidationMessage]) -> bool {
    let error_count = messages.iter().filter(|m| m.is_error()).count();
    let warning_count = messages.iter().filter(|m| !m.is_error()).count();

    if messages.is_empty() {
        tracing::info!("✓ Config validation passed — no issues found");
        return false;
    }

    if error_count > 0 {
        tracing::error!("── Config Errors ({error_count}):");
        for msg in messages.iter().filter(|m| m.is_error()) {
            match msg {
                ValidationMessage::Error(t) => tracing::error!("  ✗ {t}"),
                ValidationMessage::Warning(t) => tracing::error!("  ✗ {t}"),
            }
        }
    }

    if warning_count > 0 {
        tracing::warn!("── Config Warnings ({warning_count}):");
        for msg in messages.iter().filter(|m| !m.is_error()) {
            match msg {
                ValidationMessage::Warning(t) => tracing::warn!("  ⚠ {t}"),
                ValidationMessage::Error(t) => tracing::warn!("  ⚠ {t}"),
            }
        }
    }

    error_count > 0
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    #[test]
    fn test_validate_default_config() {
        let config = Config::default();
        let data_dir = Path::new("./data");
        let messages = validate_config(&config, data_dir);
        // Default config may have warnings (missing API key) but should not crash
        assert!(
            !messages.iter().any(|m| matches!(m, ValidationMessage::Error(s) if s.contains("risk_per_trade"))),
            "Default risk_per_trade should be valid"
        );
    }

    #[test]
    fn test_empty_symbols_warning() {
        let mut config = Config::default();
        config.market_data.symbols.clear();
        let data_dir = Path::new("/tmp");
        let messages = validate_config(&config, data_dir);
        assert!(
            messages.iter().any(|m| matches!(m, ValidationMessage::Warning(s) if s.contains("No market symbols"))),
            "Should warn about empty symbols"
        );
    }

    #[test]
    fn test_negative_risk_error() {
        let mut config = Config::default();
        config.strategy.risk_per_trade = -0.1;
        let data_dir = Path::new("/tmp");
        let messages = validate_config(&config, data_dir);
        assert!(
            messages.iter().any(|m| matches!(m, ValidationMessage::Error(s) if s.contains("risk_per_trade"))),
            "Should error on negative risk"
        );
    }

    #[test]
    fn test_print_no_issues() {
        let messages = vec![];
        let has_errors = print_validation(&messages);
        assert!(!has_errors);
    }

    #[test]
    fn test_print_with_errors() {
        let messages = vec![
            ValidationMessage::Error("Test error".into()),
            ValidationMessage::Warning("Test warning".into()),
        ];
        let has_errors = print_validation(&messages);
        assert!(has_errors);
    }
}
