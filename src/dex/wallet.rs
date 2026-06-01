//! Solana wallet management — keypair loading and address derivation.
//!
//! For Stage 6, this is an architectural scaffold. Actual signing
//! and RPC submission will be wired in Stage 7 (Live Trading).
//!
//! Keypairs are stored as raw 64-byte ed25519 seeds (BS58-encoded).

use std::path::Path;

/// A loaded Solana keypair.
#[derive(Debug, Clone)]
pub struct Wallet {
    /// Public key (32 bytes)
    pub pubkey: [u8; 32],
    /// Secret key (64 bytes, includes public key)
    pub secret: [u8; 64],
}

impl Wallet {
    /// Create a wallet from a 64-byte ed25519 secret key array.
    pub fn from_bytes(secret: [u8; 64]) -> Self {
        let pubkey = {
            let mut pk = [0u8; 32];
            pk.copy_from_slice(&secret[32..64]);
            pk
        };
        Self { pubkey, secret }
    }

    /// Load a keypair from a BS58-encoded private key string.
    ///
    /// The string can be:
    /// - A 64-byte secret key (BS58 encoded)
    /// - A 32-byte seed (BS58 encoded, for key derivation)
    pub fn from_bs58(encoded: &str) -> anyhow::Result<Self> {
        let bytes = bs58::decode(encoded)
            .into_vec()
            .map_err(|e| anyhow::anyhow!("Invalid BS58 key: {e}"))?;

        match bytes.len() {
            64 => {
                let mut secret = [0u8; 64];
                secret.copy_from_slice(&bytes);
                Ok(Self::from_bytes(secret))
            }
            32 => {
                // Treat as seed — not a full keypair
                anyhow::bail!(
                    "32-byte seed provided but 64-byte secret key required. \
                     Use `solana-keygen recover` to get the full keypair."
                );
            }
            n => {
                anyhow::bail!(
                    "Invalid key length: expected 64 bytes, got {n} bytes"
                );
            }
        }
    }

    /// Load a keypair from a JSON keypair file (Solana CLI format).
    pub fn from_json_file(path: &Path) -> anyhow::Result<Self> {
        let content = std::fs::read_to_string(path)
            .map_err(|e| anyhow::anyhow!("Failed to read keypair file {path:?}: {e}"))?;

        let bytes: Vec<u8> = serde_json::from_str(&content)
            .map_err(|e| anyhow::anyhow!("Invalid JSON keypair format: {e}"))?;

        if bytes.len() != 64 {
            anyhow::bail!("Expected 64 bytes, got {}", bytes.len());
        }

        let mut secret = [0u8; 64];
        secret.copy_from_slice(&bytes);
        Ok(Self::from_bytes(secret))
    }

    /// Get the base58-encoded public key.
    pub fn pubkey_bs58(&self) -> String {
        bs58::encode(&self.pubkey).into_string()
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// A known test keypair (RUSTSEC-2025-xxx test vector, DO NOT USE FOR REAL FUNDS)
    const TEST_SECRET: [u8; 64] = [
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ];

    #[test]
    fn test_wallet_from_bytes() {
        let wallet = Wallet::from_bytes(TEST_SECRET);
        assert_eq!(wallet.pubkey[0], 1);
        // pubkey is bytes 32..64 of the secret
        assert_eq!(wallet.pubkey[0], TEST_SECRET[32]);
    }

    #[test]
    fn test_pubkey_bs58() {
        let wallet = Wallet::from_bytes(TEST_SECRET);
        let address = wallet.pubkey_bs58();
        // First byte is 1, rest zeros → should start with specific BS58 pattern
        assert!(!address.is_empty());
        assert!(address.len() >= 32); // Solana addresses are 32-44 chars
    }

    #[test]
    fn test_from_bs58_roundtrip() {
        // Encode the test secret, then decode it
        let encoded = bs58::encode(&TEST_SECRET[..]).into_string();
        let wallet = Wallet::from_bs58(&encoded).unwrap();
        assert_eq!(wallet.secret, TEST_SECRET);
    }

    #[test]
    fn test_invalid_bs58() {
        assert!(Wallet::from_bs58("!!!not_valid!!!").is_err());
    }

    #[test]
    fn test_short_key() {
        // 32 bytes is not a full keypair
        let short = bs58::encode(&[0u8; 32]).into_string();
        assert!(Wallet::from_bs58(&short).is_err());
    }
}
