---
source: Context7 API
library: KuCoin API
package: kucoin
topic: websocket-market-data
fetched: 2026-04-14T00:00:00Z
official_docs: https://www.kucoin.com/docs-new/?lang=en_US
---

# KuCoin WebSocket API - Real-Time Market Data

## Connection Overview

### Step 1: Get WebSocket Token
**GET** `https://api.kucoin.com/api/v1/bullet-public`

No authentication required for public data streams.

**Response:**
```json
{
  "code": "OK",
  "data": {
    "instanceServers": [
      {
        "pingInterval": 30,
        "pingTimeout": 10,
        "endpoint": "wss://ws-api.kucoin.com/v1/endpoint",
        "protocol": "websocket",
        "encrypt": true
      }
    ],
    "token": "YOUR_WEBSOCKET_TOKEN"
  }
}
```

### Step 2: Connect to WebSocket
Connect to the `endpoint` URL with token as query parameter:
```
wss://ws-api.kucoin.com/v1/endpoint?token=YOUR_TOKEN
```

### Step 3: Subscribe to Topics
Send subscription message:
```json
{
  "id": 1545910660739,
  "type": "subscribe",
  "topic": "/market/ticker:BTC-USDT",
  "response": true
}
```

**Note:** Token expires in 24 hours. Reconnect and get new token if disconnected.

---

## Public WebSocket Topics

### /market/ticker:{symbol}
Real-time ticker for a specific symbol.

**Subscribe:**
```json
{
  "id": 1545910660739,
  "type": "subscribe",
  "topic": "/market/ticker:BTC-USDT",
  "response": true
}
```

**Push Data:**
```json
{
  "type": "message",
  "topic": "/market/ticker:BTC-USDT",
  "data": {
    "symbol": "BTC-USDT",
    "buy": "30000.00",
    "sell": "30001.00",
    "last": "30000.50",
    "vol": "1000.00"
  }
}
```

---

### /market/ticker:all
All symbols ticker (BBO - Best Bid Offer).

**Subscribe:**
```json
{
  "id": 1545910660739,
  "type": "subscribe",
  "topic": "/market/ticker:all",
  "response": true
}
```

**Note:** Push updates delivered once every 100ms.

---

### /market/candles:{symbol}_{type}
Real-time K-Line (candlestick) data.

**Subscribe:**
```json
{
  "id": 1545910660739,
  "type": "subscribe",
  "topic": "/market/candles:BTC-USDT_1hour",
  "response": true
}
```

**Interval Types:** 1min, 3min, 15min, 30min, 1hour, 2hour, 4hour, 6hour, 8hour, 12hour, 1day, 1week

**Push Data:**
```json
{
  "topic": "/market/candles:BTC-USDT_1hour",
  "type": "message",
  "subject": "trade.candles.update",
  "data": {
    "symbol": "BTC-USDT",
    "candles": ["1729839600", "67644.9", "67437.6", "67724.8", "67243.8", "44.88321441", "3027558.991928447"],
    "time": 1729842192785164840
  }
}
```
Array: [startTime, openPrice, closePrice, highPrice, lowPrice, volume, turnover]

---

### /market/level2:{symbol}
Order book updates (L2 - Level 2).

**Subscribe:**
```json
{
  "id": 1545910660739,
  "type": "subscribe",
  "topic": "/market/level2:BTC-USDT",
  "response": true
}
```

---

### /market/match:{symbol}
Trade history (real-time trades).

**Subscribe:**
```json
{
  "id": 1545910660739,
  "type": "subscribe",
  "topic": "/market/match:BTC-USDT",
  "response": true
}
```

---

## Unsubscribe
```json
{
  "id": 1545910660739,
  "type": "unsubscribe",
  "topic": "/market/ticker:BTC-USDT",
  "response": true
}
```

---

## Ping/Pong
Server sends ping every 30 seconds (pingInterval). Client should respond with pong.

---

## Symbol Limits
- Up to 100 symbols per topic subscription
- For more symbols, make multiple subscriptions
