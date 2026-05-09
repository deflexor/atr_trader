/// Bybit V5 REST API client — replaces ccxt.pro.bybit.
///
/// Implements authenticated HTTP calls for order placement, market data,
/// account queries, and perpetual symbol setup.

use reqwest::Client;
use std::time::Duration;
use tokio::time::sleep;
use tracing::{debug, info, warn};

use super::auth;

/// Custom error type for exchange operations.
#[derive(Debug, thiserror::Error)]
pub enum ExchangeError {
    #[error("API error: {0}")]
    Api(String),
    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("Rate limited after retries")]
    RateLimited,
}

/// Normalized order response from the exchange.
#[derive(Debug, Clone)]
pub struct OrderResponse {
    pub order_id: String,
    pub status: String,
    pub price: Option<f64>,
    pub quantity: Option<f64>,
    pub avg_fill_price: Option<f64>,
}

/// Bybit V5 REST API client.
#[derive(Clone)]
pub struct ExchangeClient {
    http: Client,
    api_key: String,
    api_secret: String,
    base_url: String,
    market_type: String,
    leverage: u32,
    recv_window: &'static str,
}

impl ExchangeClient {
    pub fn new(
        api_key: String,
        api_secret: String,
        testnet: bool,
        market_type: &str,
        leverage: u32,
    ) -> Self {
        let base_url = if testnet {
            "https://api-testnet.bybit.com".to_string()
        } else {
            "https://api.bybit.com".to_string()
        };
        Self {
            http: Client::builder()
                .timeout(Duration::from_secs(10))
                .build()
                .unwrap_or_default(),
            api_key,
            api_secret,
            base_url,
            market_type: market_type.to_string(),
            leverage,
            recv_window: "20000",
        }
    }

    // ── Public endpoints ──────────────────────────────────────

    /// Fetch OHLCV kline data. Returns raw JSON rows.
    pub async fn fetch_ohlcv(
        &self,
        symbol: &str,
        timeframe: &str,
        since: Option<i64>,
        limit: Option<u32>,
    ) -> Result<Vec<Vec<serde_json::Value>>, ExchangeError> {
        let ccxt_symbol = normalize_symbol(symbol, &self.market_type);
        let mut params = vec![
            ("category".to_string(), "linear".to_string()),
            ("symbol".to_string(), ccxt_symbol.clone()),
            ("interval".to_string(), timeframe.to_string()),
        ];
        if let Some(s) = since {
            params.push(("start".to_string(), s.to_string()));
        }
        if let Some(l) = limit {
            params.push(("limit".to_string(), l.to_string()));
        }

        let resp = self.get_public("/v5/market/kline", &params).await?;
        let list = resp["result"]["list"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        // Bybit returns newest first; reverse to oldest first
        let mut rows: Vec<_> = list.into_iter().map(|v| {
            v.as_array().cloned().unwrap_or_default()
        }).collect();
        rows.reverse();
        Ok(rows)
    }

    /// Fetch order book.
    pub async fn fetch_orderbook(
        &self,
        symbol: &str,
        limit: Option<u32>,
    ) -> Result<OrderBook, ExchangeError> {
        let ccxt_symbol = normalize_symbol(symbol, &self.market_type);
        let mut params = vec![
            ("category".to_string(), "linear".to_string()),
            ("symbol".to_string(), ccxt_symbol),
        ];
        if let Some(l) = limit {
            params.push(("limit".to_string(), l.to_string()));
        }

        let resp = self.get_public("/v5/market/orderbook", &params).await?;
        let result = &resp["result"];

        let bids = result["bids"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|l| {
                        let price = l[0].as_str().and_then(|s| s.parse::<f64>().ok())?;
                        let qty = l[1].as_str().and_then(|s| s.parse::<f64>().ok())?;
                        Some([price, qty])
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();

        let asks = result["asks"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|l| {
                        let price = l[0].as_str().and_then(|s| s.parse::<f64>().ok())?;
                        let qty = l[1].as_str().and_then(|s| s.parse::<f64>().ok())?;
                        Some([price, qty])
                    })
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();

        Ok(OrderBook { bids, asks })
    }

    /// Fetch ticker data.
    pub async fn fetch_ticker(&self, symbol: &str) -> Result<Ticker, ExchangeError> {
        let ccxt_symbol = normalize_symbol(symbol, &self.market_type);
        let params = vec![
            ("category".to_string(), "linear".to_string()),
            ("symbol".to_string(), ccxt_symbol),
        ];

        let resp = self.get_public("/v5/market/tickers", &params).await?;
        let t = &resp["result"]["list"][0];

        Ok(Ticker {
            bid: t["bid1Price"].as_str().and_then(|s| s.parse().ok()),
            ask: t["ask1Price"].as_str().and_then(|s| s.parse().ok()),
            last: t["lastPrice"].as_str().and_then(|s| s.parse().ok()),
            high: t["highPrice24h"].as_str().and_then(|s| s.parse().ok()),
            low: t["lowPrice24h"].as_str().and_then(|s| s.parse().ok()),
            volume: t["volume24h"].as_str().and_then(|s| s.parse().ok()),
        })
    }

    // ── Private endpoints ──────────────────────────────────────

    /// Initialize — load markets (verify connectivity).
    pub async fn start(&self) -> Result<(), ExchangeError> {
        let resp = self
            .get_public(
                "/v5/market/instruments-info",
                &[("category".to_string(), "linear".to_string())],
            )
            .await?;
        let count = resp["result"]["list"]
            .as_array()
            .map(|a| a.len())
            .unwrap_or(0);
        info!(markets = count, "exchange_started");
        Ok(())
    }

    /// Configure a symbol for USDT perpetual trading.
    pub async fn setup_perp_symbol(&self, symbol: &str) -> Result<(), ExchangeError> {
        if self.market_type != "perp" {
            return Ok(());
        }
        let ccxt_symbol = normalize_symbol(symbol, "perp");
        let leverage = self.leverage.to_string();

        // 1. Set one-way position mode
        match self
            .post_private(
                "/v5/position/switch-mode",
                serde_json::json!({
                    "category": "linear",
                    "symbol": ccxt_symbol,
                    "mode": 0, // one-way
                    "coin": "",
                }),
            )
            .await
        {
            Ok(_) => debug!(symbol = ccxt_symbol, "position_mode_set"),
            Err(e) => {
                let msg = e.to_string();
                if !msg.contains("not modified") {
                    warn!(symbol = ccxt_symbol, error = %msg, "position_mode_failed");
                }
            }
        }

        // 2. Set isolated margin mode
        match self
            .post_private(
                "/v5/account/set-margin-mode",
                serde_json::json!({
                    "category": "linear",
                    "symbol": ccxt_symbol,
                    "tradeMode": 1, // isolated
                    "buyLeverage": &leverage,
                    "sellLeverage": &leverage,
                }),
            )
            .await
        {
            Ok(_) => debug!(symbol = ccxt_symbol, "margin_mode_set"),
            Err(e) => {
                let msg = e.to_string();
                if !msg.contains("not modified") {
                    warn!(symbol = ccxt_symbol, error = %msg, "margin_mode_failed");
                }
            }
        }

        // 3. Set leverage
        match self
            .post_private(
                "/v5/position/set-leverage",
                serde_json::json!({
                    "category": "linear",
                    "symbol": ccxt_symbol,
                    "buyLeverage": &leverage,
                    "sellLeverage": &leverage,
                }),
            )
            .await
        {
            Ok(_) => info!(symbol = ccxt_symbol, leverage = %leverage, "perp_configured"),
            Err(e) => {
                let msg = e.to_string();
                if !msg.contains("not modified") {
                    warn!(symbol = ccxt_symbol, error = %msg, "leverage_set_failed");
                }
            }
        }

        Ok(())
    }

    /// Place a limit order.
    pub async fn place_limit_order(
        &self,
        symbol: &str,
        side: &str,
        quantity: f64,
        price: f64,
        reduce_only: bool,
    ) -> Result<OrderResponse, ExchangeError> {
        let ccxt_symbol = normalize_symbol(symbol, &self.market_type);
        let mut body = serde_json::json!({
            "category": "linear",
            "symbol": ccxt_symbol,
            "side": side,
            "orderType": "Limit",
            "qty": format!("{:.6}", quantity),
            "price": format!("{:.2}", price),
        });
        if reduce_only {
            body["reduceOnly"] = serde_json::json!(true);
        }

        let resp = self.post_private_with_retry("/v5/order/create", body).await?;
        let order_id = resp["result"]["orderId"]
            .as_str()
            .unwrap_or("unknown")
            .to_string();

        info!(
            symbol = ccxt_symbol,
            side = side,
            price = format!("{:.2}", price),
            quantity = format!("{:.6}", quantity),
            order_id = %order_id,
            "limit_order_placed"
        );

        Ok(OrderResponse {
            order_id,
            status: "open".to_string(),
            price: Some(price),
            quantity: Some(quantity),
            avg_fill_price: None,
        })
    }

    /// Place a market order.
    pub async fn place_market_order(
        &self,
        symbol: &str,
        side: &str,
        quantity: f64,
        reduce_only: bool,
    ) -> Result<OrderResponse, ExchangeError> {
        let ccxt_symbol = normalize_symbol(symbol, &self.market_type);
        let mut body = serde_json::json!({
            "category": "linear",
            "symbol": ccxt_symbol,
            "side": side,
            "orderType": "Market",
            "qty": format!("{:.6}", quantity),
        });
        if reduce_only {
            body["reduceOnly"] = serde_json::json!(true);
        }

        let resp = self.post_private_with_retry("/v5/order/create", body).await?;
        let order_id = resp["result"]["orderId"]
            .as_str()
            .unwrap_or("unknown")
            .to_string();

        info!(
            symbol = ccxt_symbol,
            side = side,
            quantity = format!("{:.6}", quantity),
            order_id = %order_id,
            "market_order_placed"
        );

        // Market orders fill immediately; fetch fill details
        let fill = self.fetch_order_status(symbol, &order_id).await.ok();
        Ok(OrderResponse {
            order_id,
            status: fill.as_ref().map(|f| f.status.clone()).unwrap_or_else(|| "open".to_string()),
            price: None,
            quantity: Some(quantity),
            avg_fill_price: fill.and_then(|f| f.avg_fill_price),
        })
    }

    /// Cancel an open order.
    pub async fn cancel_order(
        &self,
        symbol: &str,
        order_id: &str,
    ) -> Result<bool, ExchangeError> {
        let ccxt_symbol = normalize_symbol(symbol, &self.market_type);

        // Check current status first
        let status = self.fetch_order_status(symbol, order_id).await;
        if let Ok(s) = &status {
            if matches!(s.status.as_str(), "Filled" | "Cancelled") {
                debug!(symbol = ccxt_symbol, order_id = %order_id, "cancel_skipped_already_terminal");
                return Ok(true);
            }
        }

        let body = serde_json::json!({
            "category": "linear",
            "symbol": ccxt_symbol,
            "orderId": order_id,
        });

        match self.post_private("/v5/order/cancel", body).await {
            Ok(_) => {
                info!(symbol = ccxt_symbol, order_id = %order_id, "order_cancelled");
                Ok(true)
            }
            Err(ExchangeError::Api(msg)) if msg.contains("not exist") => {
                info!(symbol = ccxt_symbol, order_id = %order_id, "cancel_idempotent");
                Ok(true)
            }
            Err(e) => Err(e),
        }
    }

    /// Fetch order status by order ID.
    pub async fn fetch_order_status(
        &self,
        symbol: &str,
        order_id: &str,
    ) -> Result<OrderStatus, ExchangeError> {
        let ccxt_symbol = normalize_symbol(symbol, &self.market_type);
        let params = vec![
            ("category".to_string(), "linear".to_string()),
            ("symbol".to_string(), ccxt_symbol),
            ("orderId".to_string(), order_id.to_string()),
        ];

        let resp = self.get_private("/v5/order/realtime", &params).await?;
        let order = &resp["result"]["list"][0];

        let status = order["orderStatus"]
            .as_str()
            .unwrap_or("Unknown")
            .to_string();
        let filled: f64 = order["cumExecQty"]
            .as_str()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0.0);
        let avg_fill_price: Option<f64> = order["avgPrice"]
            .as_str()
            .and_then(|s| s.parse().ok())
            .filter(|p: &f64| *p > 0.0);

        Ok(OrderStatus {
            status,
            filled,
            avg_fill_price,
        })
    }

    /// Fetch account balance.
    pub async fn fetch_balance(&self) -> Result<serde_json::Value, ExchangeError> {
        let resp = self
            .get_private(
                "/v5/account/wallet-balance",
                &[("accountType".to_string(), "UNIFIED".to_string())],
            )
            .await?;
        Ok(resp["result"].clone())
    }

    /// Fetch open orders for a symbol.
    pub async fn fetch_open_orders(
        &self,
        symbol: &str,
    ) -> Result<Vec<OpenOrder>, ExchangeError> {
        let api_symbol = normalize_symbol(symbol, &self.market_type);
        let params = vec![
            ("category".to_string(), "linear".to_string()),
            ("symbol".to_string(), api_symbol),
        ];

        let resp = self.get_private("/v5/order/realtime", &params).await?;
        let list = resp["result"]["list"].as_array().cloned().unwrap_or_default();

        let orders: Vec<OpenOrder> = list
            .into_iter()
            .filter_map(|o| {
                let qty: f64 = o["qty"].as_str().and_then(|s| s.parse().ok())?;
                Some(OpenOrder {
                    id: o["orderId"].as_str().unwrap_or("").to_string(),
                    side: o["side"].as_str().unwrap_or("").to_string().to_lowercase(),
                    price: o["price"].as_str().and_then(|s| s.parse().ok()),
                    quantity: qty,
                    status: o["orderStatus"].as_str().unwrap_or("Unknown").to_string(),
                })
            })
            .collect();

        Ok(orders)
    }

    /// Fetch current funding rate for a perpetual symbol.
    pub async fn fetch_funding_rate(
        &self,
        symbol: &str,
    ) -> Result<FundingRate, ExchangeError> {
        let api_symbol = normalize_symbol(symbol, &self.market_type);
        let params = vec![
            ("category".to_string(), "linear".to_string()),
            ("symbol".to_string(), api_symbol),
        ];

        let resp = self.get_public("/v5/market/funding/history", &params).await?;
        let list = resp["result"]["list"].as_array();

        match list.and_then(|l| l.first()) {
            Some(fr) => Ok(FundingRate {
                rate: fr["fundingRate"].as_str().and_then(|s| s.parse().ok()),
                next_time: fr["fundingRateTimestamp"].as_str().map(|s| s.to_string()),
            }),
            None => Ok(FundingRate {
                rate: None,
                next_time: None,
            }),
        }
    }

    /// Fetch open exchange positions for reconciliation.
    pub async fn fetch_exchange_positions(
        &self,
        symbols: &[String],
    ) -> Result<Vec<ExchangePosition>, ExchangeError> {
        if self.market_type != "perp" {
            return Ok(vec![]);
        }

        let mut result = Vec::new();
        for symbol in symbols {
            let ccxt_symbol = normalize_symbol(symbol, "perp");
            let params = vec![
                ("category".to_string(), "linear".to_string()),
                ("symbol".to_string(), ccxt_symbol),
            ];

            match self.get_private("/v5/position/list", &params).await {
                Ok(resp) => {
                    if let Some(list) = resp["result"]["list"].as_array() {
                        for p in list {
                            let contracts: f64 =
                                p["size"].as_str().and_then(|s| s.parse().ok()).unwrap_or(0.0);
                            if contracts <= 0.0 {
                                continue;
                            }
                            let side = p["side"].as_str().unwrap_or("Buy").to_string();
                            let entry_price: f64 = p["avgPrice"]
                                .as_str()
                                .and_then(|s| s.parse().ok())
                                .unwrap_or(0.0);
                            let unrealized: f64 = p["unrealisedPnl"]
                                .as_str()
                                .and_then(|s| s.parse().ok())
                                .unwrap_or(0.0);

                            result.push(ExchangePosition {
                                symbol: symbol.clone(),
                                side: if side == "Buy" {
                                    "long".to_string()
                                } else {
                                    "short".to_string()
                                },
                                quantity: contracts,
                                entry_price,
                                unrealized_pnl: unrealized,
                            });
                        }
                    }
                }
                Err(e) => {
                    warn!(symbol = %symbol, error = %e, "fetch_position_symbol_failed");
                }
            }
        }
        Ok(result)
    }

    // ── HTTP helpers ────────────────────────────────────────────

    async fn get_public(
        &self,
        path: &str,
        params: &[(String, String)],
    ) -> Result<serde_json::Value, ExchangeError> {
        let url = format!("{}{}", self.base_url, path);
        let resp = self
            .http
            .get(&url)
            .query(params)
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;

        check_bybit_response(&resp)
    }

    async fn get_private(
        &self,
        path: &str,
        params: &[(String, String)],
    ) -> Result<serde_json::Value, ExchangeError> {
        let timestamp = chrono::Utc::now().timestamp_millis().to_string();
        let query_string = form_urlencoded::Serializer::new(String::new())
            .extend_pairs(params)
            .finish();
        let param_str = format!(
            "{}{}{}{}",
            timestamp, self.api_key, self.recv_window, query_string
        );
        let signature = auth::sign(&param_str, &self.api_secret);

        let url = format!("{}{}?{}", self.base_url, path, query_string);
        let resp = self
            .http
            .get(&url)
            .header("X-BAPI-API-KEY", &self.api_key)
            .header("X-BAPI-TIMESTAMP", &timestamp)
            .header("X-BAPI-SIGN", signature)
            .header("X-BAPI-RECV-WINDOW", self.recv_window)
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;

        check_bybit_response(&resp)
    }

    async fn post_private(
        &self,
        path: &str,
        body: serde_json::Value,
    ) -> Result<serde_json::Value, ExchangeError> {
        let timestamp = chrono::Utc::now().timestamp_millis().to_string();
        let body_str = serde_json::to_string(&body).unwrap_or_default();
        let param_str = format!(
            "{}{}{}{}",
            timestamp, self.api_key, self.recv_window, body_str
        );
        let signature = auth::sign(&param_str, &self.api_secret);

        let url = format!("{}{}", self.base_url, path);
        let resp = self
            .http
            .post(&url)
            .header("X-BAPI-API-KEY", &self.api_key)
            .header("X-BAPI-TIMESTAMP", &timestamp)
            .header("X-BAPI-SIGN", signature)
            .header("X-BAPI-RECV-WINDOW", self.recv_window)
            .header("Content-Type", "application/json")
            .body(body_str)
            .send()
            .await?
            .json::<serde_json::Value>()
            .await?;

        check_bybit_response(&resp)
    }

    /// POST with retry on rate-limit errors (up to 3 attempts).
    async fn post_private_with_retry(
        &self,
        path: &str,
        body: serde_json::Value,
    ) -> Result<serde_json::Value, ExchangeError> {
        let mut backoff = Duration::from_secs(1);
        for attempt in 1..=3 {
            match self.post_private(path, body.clone()).await {
                Ok(resp) => return Ok(resp),
                Err(ExchangeError::Api(msg)) if msg.contains("rate limit") || msg.contains("429") => {
                    warn!(attempt = attempt, backoff_ms = backoff.as_millis() as u64, "rate_limit_hit");
                    if attempt == 3 {
                        return Err(ExchangeError::RateLimited);
                    }
                    sleep(backoff).await;
                    backoff *= 2;
                }
                Err(e) => return Err(e),
            }
        }
        Err(ExchangeError::RateLimited)
    }
}

/// Order book data.
#[derive(Debug, Clone)]
pub struct OrderBook {
    pub bids: Vec<[f64; 2]>,
    pub asks: Vec<[f64; 2]>,
}

/// Ticker data.
#[derive(Debug, Clone)]
pub struct Ticker {
    pub bid: Option<f64>,
    pub ask: Option<f64>,
    pub last: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub volume: Option<f64>,
}

/// Order status from the exchange.
#[derive(Debug, Clone)]
pub struct OrderStatus {
    pub status: String,
    pub filled: f64,
    pub avg_fill_price: Option<f64>,
}

/// Exchange position for reconciliation.
#[derive(Debug, Clone)]
pub struct ExchangePosition {
    pub symbol: String,
    pub side: String,
    pub quantity: f64,
    pub entry_price: f64,
    pub unrealized_pnl: f64,
}

/// Open order from the exchange.
#[derive(Debug, Clone)]
pub struct OpenOrder {
    pub id: String,
    pub side: String,
    pub price: Option<f64>,
    pub quantity: f64,
    pub status: String,
}

/// Funding rate data.
#[derive(Debug, Clone)]
pub struct FundingRate {
    pub rate: Option<f64>,
    pub next_time: Option<String>,
}

/// Normalize a raw symbol to Bybit V5 API format.
///
/// Bybit V5 expects raw symbols like `BTCUSDT` for perpetuals.
/// The ccxt-style format `BTC/USDT:USDT` is NOT used by the native API.
/// This function strips any `/` or `:` separators and returns the raw form.
pub fn normalize_symbol(symbol: &str, _market_type: &str) -> String {
    let clean = symbol.to_uppercase();
    // If ccxt-style "BTC/USDT:USDT", extract base + quote
    if clean.contains('/') {
        let base = clean.split('/').next().unwrap().to_string();
        // Check for settle currency after ':'
        if let Some(quote_part) = clean.split('/').nth(1) {
            let quote = quote_part.split(':').next().unwrap();
            return format!("{}{}", base, quote);
        }
    }
    // Already raw format (e.g. "BTCUSDT") or contains ':'
    clean.replace('/', "").replace(':', "").replace("USDTUSDT", "USDT")
}

/// Check Bybit response for errors.
fn check_bybit_response(resp: &serde_json::Value) -> Result<serde_json::Value, ExchangeError> {
    let ret_code = resp["retCode"].as_i64().unwrap_or(-1);
    if ret_code == 0 {
        Ok(resp.clone())
    } else {
        let msg = resp["retMsg"]
            .as_str()
            .unwrap_or("unknown error")
            .to_string();
        Err(ExchangeError::Api(format!("{}: {}", ret_code, msg)))
    }
}
