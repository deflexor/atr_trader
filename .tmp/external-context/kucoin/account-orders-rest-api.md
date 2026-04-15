---
source: Context7 API
library: KuCoin API
package: kucoin
topic: account-orders-rest-api
fetched: 2026-04-14T00:00:00Z
official_docs: https://www.kucoin.com/docs-new/?lang=en_US
---

# KuCoin REST API - Account & Orders Endpoints

## Base URL
`https://api.kucoin.com`

## Required Headers (Private Endpoints)
All private endpoints require authentication headers:
- `KC-API-KEY`: Your API Key
- `KC-API-SIGN`: Base64-encoded HMAC-SHA256 signature
- `KC-API-TIMESTAMP`: Request timestamp in milliseconds
- `KC-API-PASSPHRASE`: Encrypted passphrase
- `KC-API-KEY-VERSION`: API key version (e.g., "2")
- `Content-Type`: application/json

---

## Spot Orders

### POST /api/v1/hf/orders
Place a spot order (HF = HotFix/Fast).

**Request Body:**
```json
{
  "clientOid": "unique-order-id",
  "side": "buy",
  "symbol": "BTC-USDT",
  "type": "limit",
  "price": "30000",
  "size": "0.001"
}
```

**Parameters:**
- `clientOid` (string, optional): Custom order ID
- `side` (string, required): "buy" or "sell"
- `symbol` (string, required): Trading pair
- `type` (string, required): "limit", "market", or "stop_limit"
- `price` (string, optional): Limit price (required for limit orders)
- `size` (string, required): Order quantity
- `timeInForce` (string, optional): "GTC", "IOC", "FOK"

---

### POST /api/v1/order
Place a standard spot order.

### POST /api/v1/order/test
Simulate order placement without execution (for testing).

---

## Order Cancellation

### DELETE /api/v1/order/{orderId}
Cancel a specific order by order ID.

**Query Parameters:**
- `symbol` (string, required): Trading symbol

### DELETE /api/v1/order/client-order/{clientOid}
Cancel by client order ID.

### DELETE /api/v1/order/batch-cancel
Cancel multiple orders at once.

**Request Body:**
```json
{
  "symbol": "BTC-USDT",
  "orderIds": ["order-id-1", "order-id-2"]
}
```

### DELETE /api/v1/order/cancel-all
Cancel all open orders for a symbol.

---

## Order Queries

### GET /api/v1/orders
Get list of orders.

**Query Parameters:**
- `symbol` (string, optional): Filter by symbol
- `status` (string, optional): "active", "done"
- `side` (string, optional): "buy" or "sell"

### GET /api/v1/orders/{orderId}
Get details of a specific order.

### GET /api/v1/orders/client-order/{clientOid}
Get order by client order ID.

---

## Stop Orders

### POST /api/v1/stop-order
Place a stop order.

**Request Body:**
```json
{
  "symbol": "BTC-USDT",
  "side": "buy",
  "type": "limit",
  "price": "29000",
  "size": "0.001",
  "stop": "price",
  "stopPrice": "28000"
}
```

### GET /api/v1/get-stop-order-by-orderld/{orderId}
Get stop order by ID.

### DELETE /api/v1/stop-order/{orderId}
Cancel a stop order.

### DELETE /api/v1/stop-order/cancel-all
Cancel all stop orders.

---

## Futures Orders

### POST /api/v5/hf/futures/order
Place a futures order.

**Request Body:**
```json
{
  "symbol": "XBTUSDT",
  "side": "BUY",
  "orderType": "LIMIT",
  "price": "30000",
  "size": "100"
}
```

### POST /api/v1/order
Place a standard futures order.

### POST /api/v1/order/batch
Place multiple futures orders in one request.

### POST /api/v1/order/oco
Place OCO (One-Cancels-Other) order.

### DELETE /api/v1/order/{orderId}
Cancel futures order by ID.

### DELETE /api/v1/order/cancel-all
Cancel all open futures orders.

### DELETE /api/v1/order/cancel-stop-all
Cancel all stop orders for futures.

---

## Account Info

### GET /api/v1/accounts
Get all accounts.

### GET /api/v1/accounts/{accountId}
Get specific account details.

### GET /api/v1/trade-fees
Get trading fees (requires authentication).

---

## Order Response Codes
- `code: "200000"` = Success
- `code: "100003"` = Order parameter invalid
- `code: "200001"` = Insufficient balance
- `code: "400000"` = Symbol not found
