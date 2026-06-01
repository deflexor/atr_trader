//! Jupiter DEX client — quote fetching and swap transaction building.
//!
//! Uses the Jupiter v6 HTTP API (https://quote-api.jup.ag/v6).
//! No Solana SDK needed for quoting — pure HTTP + JSON.

use serde::{Deserialize, Serialize};

/// A route returned by the Jupiter quote API.
#[derive(Debug, Clone, Deserialize)]
pub struct Route {
    #[serde(rename = "inAmount")]
    pub in_amount: String,

    #[serde(rename = "outAmount")]
    pub out_amount: String,

    #[serde(rename = "priceImpactPct")]
    pub price_impact_pct: String,

    #[serde(rename = "marketInfos")]
    pub market_infos: Vec<MarketInfo>,

    #[serde(rename = "routePlan")]
    pub route_plan: Vec<RoutePlanStep>,

    #[serde(rename = "contextSlot")]
    pub context_slot: Option<u64>,

    #[serde(rename = "timeTaken")]
    pub time_taken: Option<f64>,
}

/// A single swap step in a route.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoutePlanStep {
    #[serde(rename = "swapInfo")]
    pub swap_info: SwapInfo,
    pub percent: u64,
}

/// Swap info for a specific DEX.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SwapInfo {
    #[serde(rename = "ammKey")]
    pub amm_key: String,

    pub label: String,

    #[serde(rename = "inputMint")]
    pub input_mint: String,

    #[serde(rename = "outputMint")]
    pub output_mint: String,

    #[serde(rename = "inAmount")]
    pub in_amount: String,

    #[serde(rename = "outAmount")]
    pub out_amount: String,

    #[serde(rename = "feeAmount")]
    pub fee_amount: String,

    #[serde(rename = "feeMint")]
    pub fee_mint: String,
}

/// Market info (from older Jupiter response format).
#[derive(Debug, Clone, Deserialize)]
pub struct MarketInfo {
    pub id: String,
    pub label: String,
    #[serde(rename = "inputMint")]
    pub input_mint: String,
    #[serde(rename = "outputMint")]
    pub output_mint: String,
    #[serde(rename = "inAmount")]
    pub in_amount: String,
    #[serde(rename = "outAmount")]
    pub out_amount: String,
    #[serde(rename = "feeAmount")]
    pub fee_amount: String,
    #[serde(rename = "feeMint")]
    pub fee_mint: String,
}

/// A complete quote response from Jupiter.
#[derive(Debug, Clone, Deserialize)]
pub struct QuoteResponse {
    #[serde(rename = "inputMint")]
    pub input_mint: String,

    #[serde(rename = "outputMint")]
    pub output_mint: String,

    #[serde(rename = "inAmount")]
    pub in_amount: String,

    #[serde(rename = "outAmount")]
    pub out_amount: String,

    #[serde(rename = "otherAmountThreshold")]
    pub other_amount_threshold: String,

    #[serde(rename = "priceImpactPct")]
    pub price_impact_pct: String,

    #[serde(rename = "routePlan")]
    pub route_plan: Vec<RoutePlanStep>,

    #[serde(rename = "contextSlot")]
    pub context_slot: Option<u64>,

    #[serde(rename = "timeTaken")]
    pub time_taken: Option<f64>,

    /// The best route (from the routes array, Jupiter picks it)
    #[serde(default)]
    pub routes: Vec<Route>,
}

/// Processed quote with parsed numeric values.
#[derive(Debug, Clone, Serialize)]
pub struct Quote {
    pub input_mint: String,
    pub output_mint: String,
    /// Input amount in smallest unit (lamports)
    pub in_amount: u64,
    /// Output amount in smallest unit
    pub out_amount: u64,
    /// Minimum output before slippage (in smallest unit)
    pub other_amount_threshold: u64,
    pub price_impact_pct: f64,
    pub route_plan: Vec<RoutePlanStep>,
    pub context_slot: Option<u64>,
    pub fee_info: Option<SwapFeeInfo>,
}

/// Fee breakdown for a swap.
#[derive(Debug, Clone, Serialize)]
pub struct SwapFeeInfo {
    pub amm_fee: f64,
    pub lp_fee: f64,
    pub platform_fee: f64,
}

/// Request body for the Jupiter swap API.
#[derive(Debug, Clone, Serialize)]
pub struct SwapRequest {
    #[serde(rename = "quoteResponse")]
    pub quote_response: serde_json::Value,

    #[serde(rename = "userPublicKey")]
    pub user_public_key: String,

    #[serde(rename = "wrapAndUnwrapSol")]
    pub wrap_and_unwrap_sol: bool,

    #[serde(rename = "dynamicComputeUnitLimit")]
    pub dynamic_compute_unit_limit: bool,

    #[serde(rename = "prioritizationFeeLamports")]
    pub prioritization_fee_lamports: PrioritizationFee,
}

/// Prioritization fee configuration.
#[derive(Debug, Clone, Serialize)]
#[serde(untagged)]
pub enum PrioritizationFee {
    Auto { auto: serde_json::Value },
    Manual { lamports: u64 },
}

impl Default for PrioritizationFee {
    fn default() -> Self {
        PrioritizationFee::Auto {
            auto: serde_json::json!({"priorityLevelWithMaxLamports": {
                "priorityLevel": "medium",
                "maxLamports": 1_000_000
            }}),
        }
    }
}

/// Response from the Jupiter swap API.
#[derive(Debug, Clone, Deserialize)]
pub struct SwapResponse {
    #[serde(rename = "swapTransaction")]
    pub swap_transaction: String,

    #[serde(rename = "lastValidBlockHeight")]
    pub last_valid_block_height: Option<u64>,

    #[serde(rename = "prioritizationFeeLamports")]
    pub prioritization_fee_lamports: Option<u64>,

    #[serde(rename = "computeUnitLimit")]
    pub compute_unit_limit: Option<u64>,
}

/// Jupiter HTTP client.
pub struct JupiterClient {
    /// Base URL for the Jupiter API
    pub base_url: String,
    /// HTTP client
    client: reqwest::Client,
}

impl JupiterClient {
    pub fn new(base_url: Option<String>) -> Self {
        Self {
            base_url: base_url.unwrap_or_else(|| "https://quote-api.jup.ag".into()),
            client: reqwest::Client::new(),
        }
    }

    /// Fetch a quote from Jupiter.
    ///
    /// `amount` is in the input token's smallest unit (lamports for SOL).
    pub async fn get_quote(
        &self,
        input_mint: &str,
        output_mint: &str,
        amount: u64,
        slippage_bps: u64,
    ) -> anyhow::Result<Quote> {
        let url = format!(
            "{}/v6/quote?inputMint={}&outputMint={}&amount={}&slippageBps={}",
            self.base_url, input_mint, output_mint, amount, slippage_bps,
        );

        let resp = self.client.get(&url).send().await?;
        let text = resp.text().await?;

        let quote_resp: QuoteResponse = serde_json::from_str(&text)
            .map_err(|e| anyhow::anyhow!("Failed to parse Jupiter quote: {e}\nResponse: {text}"))?;

        let quote = Quote {
            input_mint: quote_resp.input_mint,
            output_mint: quote_resp.output_mint,
            in_amount: quote_resp.in_amount.parse::<u64>()
                .map_err(|e| anyhow::anyhow!("Invalid in_amount: {e}"))?,
            out_amount: quote_resp.out_amount.parse::<u64>()
                .map_err(|e| anyhow::anyhow!("Invalid out_amount: {e}"))?,
            other_amount_threshold: quote_resp.other_amount_threshold.parse::<u64>()
                .map_err(|e| anyhow::anyhow!("Invalid other_amount_threshold: {e}"))?,
            price_impact_pct: quote_resp.price_impact_pct.parse::<f64>()
                .unwrap_or(0.0),
            route_plan: quote_resp.route_plan,
            context_slot: quote_resp.context_slot,
            fee_info: None,
        };

        Ok(quote)
    }

    /// Build a swap transaction from a quote.
    ///
    /// Returns the base64-encoded swap transaction and optional metadata.
    pub async fn build_swap(
        &self,
        quote_response: &serde_json::Value,
        user_public_key: &str,
    ) -> anyhow::Result<SwapResponse> {
        let request = SwapRequest {
            quote_response: quote_response.clone(),
            user_public_key: user_public_key.to_string(),
            wrap_and_unwrap_sol: true,
            dynamic_compute_unit_limit: true,
            prioritization_fee_lamports: PrioritizationFee::default(),
        };

        let url = format!("{}/v6/swap", self.base_url);
        let resp = self.client.post(&url).json(&request).send().await?;
        let text = resp.text().await?;

        let swap_resp: SwapResponse = serde_json::from_str(&text)
            .map_err(|e| anyhow::anyhow!("Failed to parse swap response: {e}\nResponse: {text}"))?;

        Ok(swap_resp)
    }
}

impl Default for JupiterClient {
    fn default() -> Self {
        Self::new(None)
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_quote_response_deserialize() {
        let json = r#"{
            "inputMint": "So11111111111111111111111111111111111111112",
            "inAmount": "1000000000",
            "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "outAmount": "150000000",
            "otherAmountThreshold": "149250000",
            "priceImpactPct": "0.15",
            "routePlan": [
                {
                    "swapInfo": {
                        "ammKey": "ABC123",
                        "label": "Orca",
                        "inputMint": "So11111111111111111111111111111111111111112",
                        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        "inAmount": "1000000000",
                        "outAmount": "150000000",
                        "feeAmount": "300000",
                        "feeMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                    },
                    "percent": 100
                }
            ],
            "contextSlot": 123456789,
            "timeTaken": 0.045
        }"#;

        let resp: QuoteResponse = serde_json::from_str(json).unwrap();
        assert_eq!(resp.input_mint, "So11111111111111111111111111111111111111112");
        assert_eq!(resp.in_amount, "1000000000");
        assert_eq!(resp.out_amount, "150000000");
        assert_eq!(resp.price_impact_pct, "0.15");
        assert_eq!(resp.route_plan.len(), 1);
        assert_eq!(resp.route_plan[0].swap_info.label, "Orca");
        assert_eq!(resp.context_slot, Some(123456789));
    }

    #[test]
    fn test_quote_process() {
        let json = r#"{
            "inputMint": "So11111111111111111111111111111111111111112",
            "inAmount": "1000000000",
            "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "outAmount": "150000000",
            "otherAmountThreshold": "149250000",
            "priceImpactPct": "0.15",
            "routePlan": [],
            "contextSlot": null,
            "timeTaken": null
        }"#;

        let resp: QuoteResponse = serde_json::from_str(json).unwrap();
        let quote = Quote {
            input_mint: resp.input_mint,
            output_mint: resp.output_mint,
            in_amount: resp.in_amount.parse::<u64>().unwrap(),
            out_amount: resp.out_amount.parse::<u64>().unwrap(),
            other_amount_threshold: resp.other_amount_threshold.parse::<u64>().unwrap(),
            price_impact_pct: resp.price_impact_pct.parse::<f64>().unwrap(),
            route_plan: resp.route_plan,
            context_slot: resp.context_slot,
            fee_info: None,
        };

        assert_eq!(quote.in_amount, 1_000_000_000);
        assert_eq!(quote.out_amount, 150_000_000);
        assert!((quote.price_impact_pct - 0.15).abs() < 0.001);
    }

    #[test]
    fn test_swap_request_serialize() {
        let request = SwapRequest {
            quote_response: serde_json::json!({
                "inputMint": "So11111111111111111111111111111111111111112",
                "inAmount": "1000000000",
                "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "outAmount": "150000000",
                "otherAmountThreshold": "149250000",
                "priceImpactPct": "0.15",
                "routePlan": [],
                "contextSlot": null,
                "timeTaken": null
            }),
            user_public_key: "ABC123XYZ".into(),
            wrap_and_unwrap_sol: true,
            dynamic_compute_unit_limit: true,
            prioritization_fee_lamports: PrioritizationFee::Auto {
                auto: serde_json::json!({"priorityLevelWithMaxLamports": {
                    "priorityLevel": "medium",
                    "maxLamports": 1_000_000
                }}),
            },
        };

        let json = serde_json::to_string(&request).unwrap();
        assert!(json.contains("quoteResponse"));
        assert!(json.contains("userPublicKey"));
        assert!(json.contains("wrapAndUnwrapSol"));
        assert!(json.contains("dynamicComputeUnitLimit"));
        assert!(json.contains("prioritizationFeeLamports"));
        assert!(json.contains("ABC123XYZ"));
    }

    #[test]
    fn test_swap_response_deserialize() {
        let json = r#"{
            "swapTransaction": "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "lastValidBlockHeight": 123456789,
            "prioritizationFeeLamports": 5000,
            "computeUnitLimit": 200000
        }"#;

        let resp: SwapResponse = serde_json::from_str(json).unwrap();
        assert!(resp.swap_transaction.starts_with("AQ"));
        assert_eq!(resp.last_valid_block_height, Some(123456789));
        assert_eq!(resp.prioritization_fee_lamports, Some(5000));
    }

    #[test]
    fn test_swap_response_minimal() {
        let json = r#"{
            "swapTransaction": "AAAA"
        }"#;

        let resp: SwapResponse = serde_json::from_str(json).unwrap();
        assert_eq!(resp.swap_transaction, "AAAA");
        assert!(resp.last_valid_block_height.is_none());
    }
}
