//! Swap transaction executor — submits Jupiter swap transactions to Solana RPC.
//!
//! Decodes the base64 swap transaction from Jupiter and submits it
//! to a Solana JSON-RPC endpoint. For production, signing is done
//! externally via Solana CLI tools.
//!
//! # Security
//! Private keys never leave the wallet module. The executor only
//! handles the final RPC submission of already-signed transactions.

use serde::Serialize;

/// Submit a base64-encoded transaction to a Solana RPC endpoint.
///
/// Uses the `sendTransaction` JSON-RPC method with `{encoding: "base64"}`.
pub struct SwapExecutor {
    pub rpc_url: String,
    pub dry_run: bool,
    client: reqwest::Client,
}

/// RPC request body for `sendTransaction`.
#[derive(Serialize)]
struct RpcRequest {
    jsonrpc: &'static str,
    id: u64,
    method: &'static str,
    params: Vec<serde_json::Value>,
}

impl SwapExecutor {
    pub fn new(rpc_url: String, dry_run: bool) -> Self {
        Self {
            rpc_url,
            dry_run,
            client: reqwest::Client::new(),
        }
    }

    /// Decode a base64 transaction and return the raw bytes.
    pub fn decode_transaction(encoded: &str) -> anyhow::Result<Vec<u8>> {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .map_err(|e| anyhow::anyhow!("Failed to decode base64 transaction: {e}"))
    }

    /// Submit a swap transaction to the Solana network.
    ///
    /// `swap_transaction_encoded` is the base64 transaction from Jupiter.
    ///
    /// Returns the transaction signature on success.
    pub async fn submit(&self, swap_transaction_encoded: &str) -> anyhow::Result<String> {
        let tx_bytes = Self::decode_transaction(swap_transaction_encoded)?;

        if self.dry_run {
            tracing::info!(
                "[DRY-RUN] Would submit transaction: {} bytes",
                tx_bytes.len()
            );
            tracing::info!(
                "[DRY-RUN] Use: solana confirm <signature>"
            );
            return Ok("dry_run_signature".into());
        }

        // Re-encode as base64 for the RPC call
        use base64::Engine as _;
        let encoded = base64::engine::general_purpose::STANDARD.encode(&tx_bytes);

        let request = RpcRequest {
            jsonrpc: "2.0",
            id: 1,
            method: "sendTransaction",
            params: vec![
                serde_json::Value::String(encoded),
                serde_json::json!({
                    "encoding": "base64",
                    "skipPreflight": false,
                    "preflightCommitment": "processed",
                    "maxRetries": 3,
                }),
            ],
        };

        let resp = self
            .client
            .post(&self.rpc_url)
            .json(&request)
            .send()
            .await
            .map_err(|e| anyhow::anyhow!("RPC request failed: {e}"))?;

        let status = resp.status();
        let body: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to parse RPC response: {e}"))?;

        if !status.is_success() {
            anyhow::bail!("RPC error ({}): {}", status, body);
        }

        if let Some(err) = body.get("error") {
            anyhow::bail!("RPC error: {}", err);
        }

        let signature = body["result"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("RPC response missing 'result': {body}"))?
            .to_string();

        tracing::info!("Transaction submitted: {signature}");
        Ok(signature)
    }

    /// Send a transaction and wait for confirmation.
    pub async fn submit_and_confirm(
        &self,
        swap_transaction_encoded: &str,
        max_retries: usize,
    ) -> anyhow::Result<String> {
        let signature = self.submit(swap_transaction_encoded).await?;

        if self.dry_run {
            return Ok(signature);
        }

        // Poll for confirmation
        for attempt in 0..max_retries {
            let sig = self
                .get_signature_status(&signature)
                .await
                .unwrap_or("unknown".into());

            if sig == "confirmed" || sig == "finalized" {
                tracing::info!("Transaction {signature} confirmed ({sig})");
                return Ok(signature);
            }

            if sig == "TransactionNotFound" {
                // Not seen yet — retry
            }

            tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;
        }

        tracing::warn!(
            "Transaction {signature} not confirmed after {max_retries} retries"
        );
        Ok(signature)
    }

    /// Check the status of a transaction signature.
    async fn get_signature_status(&self, signature: &str) -> Option<String> {
        let request = RpcRequest {
            jsonrpc: "2.0",
            id: 1,
            method: "getSignatureStatuses",
            params: vec![
                serde_json::json!([signature]),
                serde_json::json!({"searchTransactionHistory": true}),
            ],
        };

        let resp = self.client.post(&self.rpc_url).json(&request).send().await.ok()?;
        let body: serde_json::Value = resp.json().await.ok()?;
        let statuses = body["result"]["value"].as_array()?;
        let first = statuses.first()?;
        let confirmation = first["confirmationStatus"].as_str()?;
        Some(confirmation.to_string())
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // A minimal valid base64 string (not a real transaction)
    const TEST_TX_B64: &str = "AQABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4fICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj9AQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVpbXF1eX2BhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ent8fX5/gIGCg4SFhoeIiYqLjI2Oj5CRkpOUlZaXmJmam5ydnp+goaKjpKWmp6ipqqusra6vsLGys7S1tre4ubq7vL2+v8DBwsPExcbHyMnKy8zNzs/Q0dLT1NXW19jZ2tvc3d7f4OHi4+Tl5ufo6err7O3u7/Dx8vP09fb3+Pn6+/z9/v8=";

    #[test]
    fn test_decode_transaction() {
        let bytes = SwapExecutor::decode_transaction(TEST_TX_B64).unwrap();
        assert!(!bytes.is_empty(), "Should decode to non-empty bytes");
        // The test vector has known content at specific offsets
        assert!(bytes.len() > 10);
    }

    #[test]
    fn test_decode_invalid() {
        assert!(SwapExecutor::decode_transaction("!!!invalid!!!").is_err());
    }

    #[test]
    fn test_dry_run_submit() {
        let executor = SwapExecutor::new("https://api.mainnet-beta.solana.com".into(), true);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let sig = rt.block_on(executor.submit(TEST_TX_B64)).unwrap();
        assert_eq!(sig, "dry_run_signature");
    }

    #[test]
    fn test_executor_construct() {
        let executor = SwapExecutor::new("https://api.mainnet-beta.solana.com".into(), false);
        assert_eq!(executor.rpc_url, "https://api.mainnet-beta.solana.com");
        assert!(!executor.dry_run);
    }
}
