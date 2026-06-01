//! Solana token address registry.
//!
//! Maps common token symbols to their mint addresses and decimal places.
//! Static data — no IO needed.

use std::collections::HashMap;

/// Token metadata.
#[derive(Debug, Clone)]
pub struct TokenInfo {
    pub symbol: &'static str,
    pub name: &'static str,
    pub mint: &'static str,
    pub decimals: u8,
}

/// Static token registry.
#[derive(Debug)]
pub struct TokenRegistry {
    by_symbol: HashMap<&'static str, &'static TokenInfo>,
    by_mint: HashMap<&'static str, &'static TokenInfo>,
}

impl TokenRegistry {
    /// Create the default registry with known Solana tokens.
    pub fn new() -> Self {
        let tokens: &[TokenInfo] = &[
            TokenInfo {
                symbol: "SOL",
                name: "Solana",
                mint: "So11111111111111111111111111111111111111112",
                decimals: 9,
            },

            TokenInfo {
                symbol: "USDC",
                name: "USD Coin",
                mint: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                decimals: 6,
            },
            TokenInfo {
                symbol: "USDT",
                name: "Tether USD",
                mint: "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                decimals: 6,
            },
            TokenInfo {
                symbol: "ETH",
                name: "Wrapped Ethereum (Wormhole)",
                mint: "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
                decimals: 8,
            },
            TokenInfo {
                symbol: "BTC",
                name: "Wrapped Bitcoin (Wormhole)",
                mint: "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcommJ",
                decimals: 8,
            },
            TokenInfo {
                symbol: "BONK",
                name: "Bonk",
                mint: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                decimals: 5,
            },
            TokenInfo {
                symbol: "JUP",
                name: "Jupiter",
                mint: "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
                decimals: 6,
            },
            TokenInfo {
                symbol: "RAY",
                name: "Raydium",
                mint: "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
                decimals: 6,
            },
            TokenInfo {
                symbol: "SRM",
                name: "Serum",
                mint: "SRMuApVNdxXokk5GT7XD5cUUgXMBCoAz2LHeSBMrs1N",
                decimals: 6,
            },
        ];

        let mut by_symbol = HashMap::new();
        let mut by_mint = HashMap::new();
        for t in tokens {
            by_symbol.insert(t.symbol, t);
            by_mint.insert(t.mint, t);
        }

        Self { by_symbol, by_mint }
    }

    /// Look up a token by symbol (case-sensitive).
    pub fn by_symbol(&self, symbol: &str) -> Option<&TokenInfo> {
        self.by_symbol.get(symbol).copied()
    }

    /// Look up a token by mint address.
    pub fn by_mint(&self, mint: &str) -> Option<&TokenInfo> {
        self.by_mint.get(mint).copied()
    }

    /// Convert a human-readable amount to the smallest unit.
    ///
    /// Example: `to_lamports("SOL", 1.5)` → `1_500_000_000`
    pub fn to_lamports(&self, symbol: &str, amount: f64) -> Option<u64> {
        let info = self.by_symbol(symbol)?;
        let multiplier = 10u64.pow(info.decimals as u32);
        Some((amount * multiplier as f64) as u64)
    }

    /// Convert smallest unit to human-readable amount.
    ///
    /// Example: `from_lamports("USDC", 1_500_000)` → `1.5`
    pub fn from_lamports(&self, symbol: &str, lamports: u64) -> Option<f64> {
        let info = self.by_symbol(symbol)?;
        let divisor = 10u64.pow(info.decimals as u32);
        Some(lamports as f64 / divisor as f64)
    }
}

impl Default for TokenRegistry {
    fn default() -> Self {
        Self::new()
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resolve_sol() {
        let reg = TokenRegistry::new();
        let sol = reg.by_symbol("SOL").unwrap();
        assert_eq!(sol.mint, "So11111111111111111111111111111111111111112");
        assert_eq!(sol.decimals, 9);
    }

    #[test]
    fn test_resolve_usdc() {
        let reg = TokenRegistry::new();
        let usdc = reg.by_symbol("USDC").unwrap();
        assert_eq!(usdc.decimals, 6);
    }

    #[test]
    fn test_resolve_unknown() {
        let reg = TokenRegistry::new();
        assert!(reg.by_symbol("FAKE").is_none());
    }

    #[test]
    fn test_resolve_by_mint() {
        let reg = TokenRegistry::new();
        let sol = reg.by_mint("So11111111111111111111111111111111111111112").unwrap();
        assert_eq!(sol.symbol, "SOL");
    }

    #[test]
    fn test_to_lamports() {
        let reg = TokenRegistry::new();
        let lamports = reg.to_lamports("SOL", 1.5).unwrap();
        assert_eq!(lamports, 1_500_000_000);
    }

    #[test]
    fn test_to_lamports_usdc() {
        let reg = TokenRegistry::new();
        let lamports = reg.to_lamports("USDC", 100.0).unwrap();
        assert_eq!(lamports, 100_000_000);
    }

    #[test]
    fn test_from_lamports() {
        let reg = TokenRegistry::new();
        let amount = reg.from_lamports("USDC", 1_500_000).unwrap();
        assert!((amount - 1.5).abs() < 0.001);
    }

    #[test]
    fn test_resolve_bonk() {
        let reg = TokenRegistry::new();
        let bonk = reg.by_symbol("BONK").unwrap();
        assert_eq!(bonk.decimals, 5);
        assert!(bonk.mint.starts_with("Dez"));
    }
}
