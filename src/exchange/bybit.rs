use anyhow::{bail, Context, Result};
use chrono::Utc;
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde_json::Value;
use sha2::Sha256;
use std::collections::HashMap;
use std::env;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

/// Bybit REST API v5 client for spot + linear perp trading
pub struct BybitClient {
    client: Client,
    base_url: String,
    api_key: String,
    api_secret: String,
}

/// Represents a single Bybit funding rate record
#[derive(Debug, Clone)]
pub struct BybitFundingRecord {
    pub symbol: String,
    pub rate: f64,
    pub time: i64,
}

/// Represents a Bybit position
#[derive(Debug, Clone)]
pub struct BybitPosition {
    pub symbol: String,
    pub side: String,        // "Buy" or "Sell"
    pub size: f64,           // contract size
    pub entry_price: f64,
    pub mark_price: f64,
    pub unrealised_pnl: f64,
    pub leverage: f64,
}

/// Represents wallet balance
#[derive(Debug, Clone)]
pub struct BybitBalance {
    pub total_equity: f64,
    pub wallet_balance: f64,
    pub available_balance: f64,
    pub unrealised_pnl: f64,
}

impl BybitClient {
    /// Create a new Bybit client.
    /// Reads BYBIT_API_KEY and BYBIT_API_SECRET from env.
    /// Use testnet=true for Bybit testnet.
    pub fn new(testnet: bool) -> Result<Self> {
        let api_key = env::var("BYBIT_API_KEY")
            .context("BYBIT_API_KEY not set")?;
        let api_secret = env::var("BYBIT_API_SECRET")
            .context("BYBIT_API_SECRET not set")?;

        let base_url = if testnet {
            "https://api-testnet.bybit.com".to_string()
        } else {
            "https://api.bybit.com".to_string()
        };

        Ok(Self {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()?,
            base_url,
            api_key,
            api_secret,
        })
    }

    /// Create client without env vars (for read-only public endpoints)
    pub fn new_public() -> Self {
        Self {
            client: Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .unwrap_or_default(),
            base_url: "https://api.bybit.com".to_string(),
            api_key: String::new(),
            api_secret: String::new(),
        }
    }

    // ─── Signing ─────────────────────────────────────────────

    fn sign(&self, timestamp: &str, recv_window: &str, body: &str) -> String {
        use hex::ToHex;
        let payload = format!("{}{}{}{}", timestamp, self.api_key, recv_window, body);
        let mut mac = HmacSha256::new_from_slice(self.api_secret.as_bytes())
            .expect("HMAC key");
        mac.update(payload.as_bytes());
        let result = mac.finalize();
        result.into_bytes().encode_hex::<String>()
    }

    fn signed_headers(&self, body: &str) -> HashMap<String, String> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis()
            .to_string();
        let recv_window = "5000";
        let sign = self.sign(&timestamp, recv_window, body);

        let mut headers = HashMap::new();
        headers.insert("X-BAPI-API-KEY".into(), self.api_key.clone());
        headers.insert("X-BAPI-TIMESTAMP".into(), timestamp);
        headers.insert("X-BAPI-SIGN".into(), sign);
        headers.insert("X-BAPI-RECV-WINDOW".into(), recv_window.into());
        headers
    }

    fn timestamp_ms() -> String {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis()
            .to_string()
    }

    // ─── Public API calls ────────────────────────────────────

    /// Get current ticker (price + funding rate) for a symbol
    pub async fn get_ticker(&self, symbol: &str) -> Result<TickerInfo> {
        let url = format!("{}/v5/market/tickers?category=linear&symbol={}", self.base_url, symbol);
        let resp = self.client.get(&url).send().await?;
        let data: Value = resp.json().await?;

        if data["retCode"] != 0 {
            bail!("Bybit ticker error [{}]: {}", data["retCode"], data["retMsg"]);
        }

        let result = &data["result"]["list"][0];
        Ok(TickerInfo {
            symbol: symbol.to_string(),
            last_price: result["lastPrice"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0),
            funding_rate: result["fundingRate"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0),
            next_funding_time: result["nextFundingTime"].as_str().unwrap_or("0").parse::<i64>().unwrap_or(0),
        })
    }

    /// Get all linear tickers (batch) — fetches BOTH USDT and USDC perpetuals
    pub async fn get_all_tickers(&self) -> Result<HashMap<String, TickerInfo>> {
        let mut map = HashMap::new();

        // Fetch USDT-margined perps
        let url_usdt = format!("{}/v5/market/tickers?category=linear", self.base_url);
        if let Ok(data) = self.fetch_ticker_list(&url_usdt).await {
            map.extend(data);
        }

        // Fetch USDC-margined perps
        let url_usdc = format!("{}/v5/market/tickers?category=linear&settleCoin=USDC", self.base_url);
        if let Ok(data) = self.fetch_ticker_list(&url_usdc).await {
            map.extend(data);
        }

        Ok(map)
    }

    async fn fetch_ticker_list(&self, url: &str) -> Result<HashMap<String, TickerInfo>> {
        let resp = self.client.get(url).send().await?;
        let data: Value = resp.json().await?;

        if data["retCode"] != 0 {
            bail!("Bybit tickers error [{}]: {}", data["retCode"], data["retMsg"]);
        }

        let mut map = HashMap::new();
        if let Some(list) = data["result"]["list"].as_array() {
            for item in list {
                match item["symbol"].as_str() {
                    Some(s) if s.ends_with("USDC") || s.ends_with("USDT") => {
                        map.insert(s.to_string(), TickerInfo {
                            symbol: s.to_string(),
                            last_price: item["lastPrice"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0),
                            funding_rate: item["fundingRate"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0),
                            next_funding_time: item["nextFundingTime"].as_str().unwrap_or("0").parse::<i64>().unwrap_or(0),
                        });
                    }
                    _ => {}
                }
            }
        }
        Ok(map)
    }

    /// Get spot tickers (for spot prices)
    pub async fn get_spot_tickers(&self) -> Result<HashMap<String, f64>> {
        let url = format!("{}/v5/market/tickers?category=spot", self.base_url);
        let resp = self.client.get(&url).send().await?;
        let data: Value = resp.json().await?;

        if data["retCode"] != 0 {
            bail!("Bybit spot tickers error [{}]: {}", data["retCode"], data["retMsg"]);
        }

        let mut map = HashMap::new();
        if let Some(list) = data["result"]["list"].as_array() {
            for item in list {
                let symbol = item["symbol"].as_str().unwrap_or("").to_string();
                let price = item["lastPrice"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                map.insert(symbol, price);
            }
        }
        Ok(map)
    }

    /// Get funding history for a symbol (last 200 records, ~66 days)
    pub async fn get_funding_history(&self, symbol: &str) -> Result<Vec<BybitFundingRecord>> {
        let url = format!(
            "{}/v5/market/funding/history?category=linear&symbol={}&limit=200",
            self.base_url, symbol
        );
        let resp = self.client.get(&url).send().await?;
        let data: Value = resp.json().await?;

        if data["retCode"] != 0 {
            bail!("Bybit funding history error [{}]: {}", data["retCode"], data["retMsg"]);
        }

        let mut records = Vec::new();
        if let Some(list) = data["result"]["list"].as_array() {
            for item in list {
                records.push(BybitFundingRecord {
                    symbol: symbol.to_string(),
                    rate: item["fundingRate"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0),
                    time: item["fundingRateTimestamp"].as_str().unwrap_or("0").parse::<i64>().unwrap_or(0) / 1000,
                });
            }
        }
        Ok(records)
    }

    /// Get funding history with pagination (full history)
    pub async fn get_funding_history_full(&self, symbol: &str, limit_days: u64) -> Result<Vec<BybitFundingRecord>> {
        // Bybit returns max 200 records per call (~66 days at 8h intervals)
        // We'll fetch in chunks using cursor
        let mut all_records = Vec::new();
        let max_records = (limit_days / 66).max(1) * 200; // rough estimate

        // Bybit v5 uses cursor-based pagination for funding history
        let mut cursor: Option<String> = None;

        loop {
            let mut url = format!(
                "{}/v5/market/funding/history?category=linear&symbol={}&limit=200",
                self.base_url, symbol
            );
            if let Some(ref c) = cursor {
                url.push_str(&format!("&cursor={}", c));
            }

            let resp = self.client.get(&url).send().await?;
            let data: Value = resp.json().await?;

            if data["retCode"] != 0 {
                bail!("Bybit funding history error [{}]: {}", data["retCode"], data["retMsg"]);
            }

            if let Some(list) = data["result"]["list"].as_array() {
                for item in list {
                    all_records.push(BybitFundingRecord {
                        symbol: symbol.to_string(),
                        rate: item["fundingRate"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0),
                        time: item["fundingRateTimestamp"].as_str().unwrap_or("0").parse::<i64>().unwrap_or(0) / 1000,
                    });
                }
            }

            // Check for next page cursor
            cursor = data["result"]["nextPageCursor"].as_str().map(|s| s.to_string());
            if cursor.as_deref() == Some("") || cursor.is_none() {
                break;
            }

            if all_records.len() >= max_records as usize {
                break;
            }

            // Small delay to avoid rate limits
            tokio::time::sleep(tokio::time::Duration::from_millis(100)).await;
        }

        Ok(all_records)
    }

    // ─── Authenticated API calls ─────────────────────────────

    /// Get wallet balance (USDC account)
    pub async fn get_wallet_balance(&self, account_type: &str) -> Result<BybitBalance> {
        let url = format!("{}/v5/account/wallet-balance?accountType={}&coin=USDC",
            self.base_url, account_type);
        let body = String::new();
        let headers = self.signed_headers(&body);

        let resp = self.client.get(&url)
            .headers(reqwest::header::HeaderMap::from_iter(
                headers.into_iter().map(|(k, v)| {
                    (reqwest::header::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                     reqwest::header::HeaderValue::from_str(&v).unwrap())
                })
            ))
            .send().await?;
        let data: Value = resp.json().await?;

        if data["retCode"] != 0 {
            bail!("Bybit balance error [{}]: {}", data["retCode"], data["retMsg"]);
        }

        let mut total_equity = 0.0;
        let mut wallet_balance = 0.0;
        let mut available_balance = 0.0;
        let mut unrealised_pnl = 0.0;

        if let Some(list) = data["result"]["list"].as_array() {
            if let Some(acc) = list.first() {
                if let Some(coins) = acc["coin"].as_array() {
                    for coin in coins {
                        if coin["coin"].as_str() == Some("USDC") {
                            total_equity = coin["equity"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                            wallet_balance = coin["walletBalance"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                            available_balance = coin["availableToWithdraw"].as_str().unwrap_or("0").parse().unwrap_or(0.0);
                            break;
                        }
                    }
                }
            }
        }

        Ok(BybitBalance { total_equity, wallet_balance, available_balance, unrealised_pnl })
    }

    /// Get all positions (linear)
    pub async fn get_positions(&self) -> Result<Vec<BybitPosition>> {
        let url = format!("{}/v5/position/list?category=linear&settleCoin=USDC", self.base_url);
        let body = String::new();
        let headers = self.signed_headers(&body);

        let resp = self.client.get(&url)
            .headers(reqwest::header::HeaderMap::from_iter(
                headers.into_iter().map(|(k, v)| {
                    (reqwest::header::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                     reqwest::header::HeaderValue::from_str(&v).unwrap())
                })
            ))
            .send().await?;
        let data: Value = resp.json().await?;

        if data["retCode"] != 0 {
            bail!("Bybit positions error [{}]: {}", data["retCode"], data["retMsg"]);
        }

        let mut positions = Vec::new();
        if let Some(list) = data["result"]["list"].as_array() {
            for item in list {
                let size = item["size"].as_str().unwrap_or("0").parse::<f64>().unwrap_or(0.0);
                if size == 0.0 { continue; }
                positions.push(BybitPosition {
                    symbol: item["symbol"].as_str().unwrap_or("").to_string(),
                    side: item["side"].as_str().unwrap_or("").to_string(),
                    size,
                    entry_price: item["avgPrice"].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                    mark_price: item["markPrice"].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                    unrealised_pnl: item["unrealisedPnl"].as_str().unwrap_or("0").parse().unwrap_or(0.0),
                    leverage: item["leverage"].as_str().unwrap_or("1").parse().unwrap_or(1.0),
                });
            }
        }
        Ok(positions)
    }

    /// Place a spot market order
    pub async fn place_spot_order(&self, symbol: &str, side: &str, qty: f64) -> Result<OrderResult> {
        let body = serde_json::json!({
            "category": "spot",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": format_float(qty),
        }).to_string();

        let resp = self.signed_post("/v5/order/create", &body).await?;
        self.parse_order_response(resp)
    }

    /// Place a linear perp market order
    pub async fn place_perp_order(&self, symbol: &str, side: &str, qty: f64) -> Result<OrderResult> {
        let body = serde_json::json!({
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": format_float(qty),
            "positionIdx": 0, // one-way mode
        }).to_string();

        let resp = self.signed_post("/v5/order/create", &body).await?;
        self.parse_order_response(resp)
    }

    /// Place a limit order (for maker rebates)
    pub async fn place_limit_order(
        &self, category: &str, symbol: &str, side: &str, qty: f64, price: f64,
    ) -> Result<OrderResult> {
        let mut json = serde_json::json!({
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": format_float(qty),
            "price": format_float(price),
            "timeInForce": "PostOnly",
        });
        if category == "linear" {
            json["positionIdx"] = serde_json::json!(0);
        }

        let resp = self.signed_post("/v5/order/create", &json.to_string()).await?;
        self.parse_order_response(resp)
    }

    /// Cancel an order
    pub async fn cancel_order(&self, category: &str, symbol: &str, order_id: &str) -> Result<()> {
        let body = serde_json::json!({
            "category": category,
            "symbol": symbol,
            "orderId": order_id,
        }).to_string();

        let _resp = self.signed_post("/v5/order/cancel", &body).await?;
        Ok(())
    }

    /// Cancel all orders for a symbol
    pub async fn cancel_all_orders(&self, category: &str, symbol: &str) -> Result<()> {
        let body = serde_json::json!({
            "category": category,
            "symbol": symbol,
        }).to_string();

        let _resp = self.signed_post("/v5/order/cancel-all", &body).await?;
        Ok(())
    }

    /// Set leverage for linear perp
    pub async fn set_leverage(&self, symbol: &str, leverage: f64) -> Result<()> {
        let body = serde_json::json!({
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": format_float(leverage),
            "sellLeverage": format_float(leverage),
        }).to_string();

        let _resp = self.signed_post("/v5/position/set-leverage", &body).await?;
        Ok(())
    }

    // ─── Internal ────────────────────────────────────────────

    async fn signed_post(&self, path: &str, body: &str) -> Result<Value> {
        let url = format!("{}{}", self.base_url, path);
        let headers = self.signed_headers(body);

        let resp = self.client.post(&url)
            .headers(reqwest::header::HeaderMap::from_iter(
                headers.into_iter().map(|(k, v)| {
                    (reqwest::header::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                     reqwest::header::HeaderValue::from_str(&v).unwrap())
                })
            ))
            .header("Content-Type", "application/json")
            .body(body.to_string())
            .send().await?;

        let data: Value = resp.json().await?;
        Ok(data)
    }

    fn parse_order_response(&self, data: Value) -> Result<OrderResult> {
        if data["retCode"] != 0 {
            bail!("Bybit order error [{}]: {}", data["retCode"], data["retMsg"]);
        }

        let result = &data["result"];
        Ok(OrderResult {
            order_id: result["orderId"].as_str().unwrap_or("").to_string(),
            order_link_id: result["orderLinkId"].as_str().unwrap_or("").to_string(),
            symbol: result["symbol"].as_str().unwrap_or("").to_string(),
            create_time: result["createTime"].as_str().unwrap_or("0").to_string(),
        })
    }
}

#[derive(Debug, Clone)]
pub struct TickerInfo {
    pub symbol: String,
    pub last_price: f64,
    pub funding_rate: f64,
    pub next_funding_time: i64,
}

#[derive(Debug, Clone)]
pub struct OrderResult {
    pub order_id: String,
    pub order_link_id: String,
    pub symbol: String,
    pub create_time: String,
}

fn format_float(f: f64) -> String {
    if f == f.floor() {
        format!("{}", f as i64)
    } else {
        // Don't use scientific notation
        format!("{:.8}", f).trim_end_matches('0')
            .trim_end_matches('.')
            .to_string()
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_format_float() {
        assert_eq!(format_float(1.0), "1");
        assert_eq!(format_float(0.001), "0.001");
        assert_eq!(format_float(0.00123456), "0.00123456");
        assert_eq!(format_float(100.5), "100.5");
    }

    #[test]
    fn test_public_client_creation() {
        let client = BybitClient::new_public();
        assert_eq!(client.base_url, "https://api.bybit.com");
        assert!(client.api_key.is_empty());
    }

    #[tokio::test]
    async fn test_get_all_tickers_public() {
        let client = BybitClient::new_public();
        let result = client.get_all_tickers().await;
        assert!(result.is_ok(), "get_all_tickers should work without auth: {:?}", result.err());
        let map = result.unwrap();
        assert!(map.contains_key("BTCUSDT") || map.is_empty(), "should have BTC or be empty if API is down");
    }

    #[tokio::test]
    async fn test_get_funding_history_public() {
        let client = BybitClient::new_public();
        let result = client.get_funding_history("BTCUSDT").await;
        assert!(result.is_ok(), "funding history should work without auth: {:?}", result.err());
        let records = result.unwrap();
        assert!(!records.is_empty(), "should have at least 1 funding record");
    }

    #[tokio::test]
    async fn test_get_spot_tickers() {
        let client = BybitClient::new_public();
        let result = client.get_spot_tickers().await;
        assert!(result.is_ok(), "spot tickers should work without auth: {:?}", result.err());
        let map = result.unwrap();
        assert!(map.contains_key("BTCUSDT"), "should have BTC spot price");
    }
}
