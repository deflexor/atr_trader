---
source: Context7 API
library: KuCoin API
package: kucoin
topic: rate-limits
fetched: 2026-04-14T00:00:00Z
official_docs: https://www.kucoin.com/docs-new/?lang=en_US
---

# KuCoin API Rate Limits

## Rate Limit Headers

Each API response includes rate limit information in headers:

| Header | Description |
|--------|-------------|
| `gw-ratelimit-limit` | Total resource pool quota |
| `gw-ratelimit-remaining` | Remaining quota in current cycle |
| `gw-ratelimit-reset` | Milliseconds until quota resets |

---

## Public Endpoints

- **Rate limiting**: Based on IP address
- **Recommendation**: Use WebSocket API for high-frequency public data access
- **Alternative**: Bind multiple IPs to server (supports IPv4 and IPv6)

---

## Private Endpoints

- **Rate limiting**: Based on User ID (UID)
- **Sub-accounts**: Independent rate limits from master account
- **Distribution**: Can distribute requests across multiple accounts

---

## Rate Limit Increase

Professional traders and market makers can request higher limits:

1. Contact: `api@kucoin.com`
2. Include:
   - Account details
   - Use case description
   - Estimated trading volume

---

## Error Response (429)

When rate limit exceeded:

```json
{
  "code": "429000",
  "id": "order-1741590647179",
  "op": "futures.order",
  "msg": "Too many requests in a short period of time, please retry later.",
  "inTime": 1741589852255,
  "outTime": 1741589852355,
  "rateLimit": {
    "limit": 1600,
    "reset": 15244,
    "remaining": 1528
  }
}
```

---

## Best Practices

1. **Implement exponential backoff** for retry logic
2. **Cache public data** when possible
3. **Use WebSocket** for real-time data instead of polling REST
4. **Monitor headers** to track quota consumption
5. **Distribute load** across sub-accounts for high-volume trading
