---
source: Context7 API
library: KuCoin API
package: kucoin
topic: market-data-rest-api
fetched: 2026-04-14T00:00:00Z
official_docs: https://www.kucoin.com/docs-new/?lang=en_US
---

# KuCoin REST API - Market Data Endpoints

## Base URL
`https://api.kucoin.com`

## Public Endpoints (No Authentication Required)

### Ticker Endpoints

#### GET /api/v1/market/ticker
Retrieves ticker information for a specific symbol or all symbols.

**Query Parameters:**
- `symbol` (string, optional): Trading symbol (e.g., BTC-USDT). If not provided, returns all tickers.

**Response:**
```json
{
  "code": "200000",
  "data": [
    {
      "symbol": "BTC-USDT",
      "buy": "30000.00",
      "sell": "30001.00",
      "last": "30000.50",
      "vol": "1000.00",
      "rose_down_24": "-1.5",
      "change_price": "-450.00",
      "high": "31000.00",
      "low": "29500.00"
    }
  ]
}
```

#### GET /api/v1/ticker/all
Retrieves ticker information for all trading symbols.

---

### Trade History

#### GET /api/v1/market/trade
Retrieves recent trade history for a symbol.

**Query Parameters:**
- `symbol` (string, required): Trading symbol (e.g., BTC-USDT)
- `limit` (integer, optional): Max trades to return (default: 100)

**Response:**
```json
{
  "code": "200000",
  "data": [
    {
      "time": 1678886400000,
      "price": "30000.50",
      "size": "0.1",
      "side": "BUY"
    }
  ]
}
```

---

### Klines (Candlestick Data)

#### GET /api/v1/market/klines
Retrieves candlestick data for a symbol.

**Query Parameters:**
- `symbol` (string, required): Trading symbol
- `interval` (string, required): K-line interval (1min, 5min, 15min, 30min, 1hour, 2hour, 4hour, 6hour, 12hour, 1day, 1week, 1month)
- `from` (integer, optional): Start timestamp (milliseconds)
- `to` (integer, optional): End timestamp (milliseconds)

**Response:**
```json
{
  "code": "200000",
  "data": [
    [1678857600000, "30000.00", "30100.00", "30200.00", "29900.00", "0.5", "15050.00"]
  ]
}
```
Array contains: [timestamp, open, close, high, low, volume, turnover]

---

### Order Book Endpoints

#### GET /api/v1/orderbook/part
Retrieves partial order book (recommended for most use cases).

**Query Parameters:**
- `symbol` (string, required): Trading symbol
- `limit` (integer, optional): Number of bids/asks to return

#### GET /api/v1/orderbook/full
Retrieves full order book (use with caution - large payload).

**Query Parameters:**
- `symbol` (string, required): Trading symbol

---

### Additional Market Data Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/symbols` | GET | Get all trading symbols |
| `/api/v1/symbols/{symbol}` | GET | Get specific symbol info |
| `/api/v1/currencies` | GET | Get all currencies |
| `/api/v1/currencies/{currency}` | GET | Get specific currency |
| `/api/v1/price` | GET | Get fiat price |
| `/api/v1/stats/all` | GET | 24hr trading stats |
| `/api/v1/timestamp` | GET | Server time |
| `/api/v1/status` | GET | Service status |

---

## Futures Market Data Endpoints

### GET /api/v1/contracts/active
Retrieves all active futures contracts.

### GET /api/v1/ticker?symbol={symbol}
Retrieves ticker for a specific futures contract.

### GET /api/v1/level2/depth?symbol={symbol}&depth={depth}
Retrieves order book depth.
- `depth`: Number of levels (5, 20, 50)

### GET /api/v1/trade/history?symbol={symbol}
Retrieves futures trade history.
