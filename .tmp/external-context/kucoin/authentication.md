---
source: Context7 API
library: KuCoin API
package: kucoin
topic: authentication
fetched: 2026-04-14T00:00:00Z
official_docs: https://www.kucoin.com/docs-new/?lang=en_US
---

# KuCoin API Authentication

## API Key Setup

1. Generate API Key pair on [KuCoin website](https://www.kucoin.com/account/api)
2. You will receive:
   - **Key**: Your API Key
   - **Secret**: Your API Secret
   - **Passphrase**: Your chosen passphrase (encrypted)

Store credentials securely - they cannot be recovered.

---

## Required Headers

All private REST API requests must include:

| Header | Description |
|--------|-------------|
| `KC-API-KEY` | Your unique API Key |
| `KC-API-SIGN` | Base64-encoded HMAC-SHA256 signature |
| `KC-API-TIMESTAMP` | Request timestamp in milliseconds |
| `KC-API-PASSPHRASE` | Encrypted API passphrase |
| `KC-API-KEY-VERSION` | API key version (check your API page) |
| `Content-Type` | Must be `application/json` |

---

## Signature Generation (KC-API-SIGN)

### Step 1: Construct Prehash String
```
{timestamp+method+endpoint+body}
```

Components:
- `timestamp`: Milliseconds since epoch (must match KC-API-TIMESTAMP header)
- `method`: HTTP method in UPPER CASE (GET, POST, DELETE)
- `endpoint`: API path (e.g., `/api/v1/orders`)
- `body`: Request body as JSON string (empty string `""` for GET/DELETE with no body)

### Step 2: Encrypt with HMAC-SHA256
Use your API Secret as the key to encrypt the prehash string.

### Step 3: Base64 Encode
Encode the resulting ciphertext in Base64.

---

## Passphrase Encryption (KC-API-PASSPHRASE)

1. Encrypt your passphrase using HMAC-SHA256 with your API Secret
2. Base64 encode the result

---

## Example Headers

```json
{
  "KC-API-TIMESTAMP": "1680885532722",
  "KC-API-KEY": "6422da9c97b45100018c6e62",
  "KC-API-SIGN": "ncPuAcZW8WYUZyvblRVVgMfYoVH+FlCTO6K45/FMLFQ=",
  "KC-API-PASSPHRASE": "rl1Ki0WuwidRT48JnoGQo+AJ4UtZ6mQEKt6F5XYVnT4=",
  "KC-API-KEY-VERSION": "2",
  "Content-Type": "application/json"
}
```

---

## Python Implementation

```python
import base64
import hmac
import hashlib
import time

class KcSigner:
    def __init__(self, api_key: str, api_secret: str, api_passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        # Encrypt passphrase with API secret
        self.api_passphrase = base64.b64encode(
            hmac.new(api_secret.encode(), api_passphrase.encode(), hashlib.sha256).digest()
        ).decode()

    def sign(self, plain: bytes, key: bytes) -> str:
        hm = hmac.new(key, plain, hashlib.sha256)
        return base64.b64encode(hm.digest()).decode()

    def headers(self, plain: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        signature = self.sign((timestamp + plain).encode(), self.api_secret.encode())
        return {
            "KC-API-KEY": self.api_key,
            "KC-API-PASSPHRASE": self.api_passphrase,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-SIGN": signature,
            "KC-API-KEY-VERSION": "2"
        }
```

---

## Go Implementation

```go
type KcSigner struct {
    apiKey        string
    apiSecret     string
    apiPassPhrase string
}

func Sign(plain []byte, key []byte) []byte {
    hm := hmac.New(sha256.New, key)
    hm.Write(plain)
    return []byte(base64.StdEncoding.EncodeToString(hm.Sum(nil)))
}

func (ks *KcSigner) Headers(plain string) map[string]string {
    t := strconv.FormatInt(time.Now().UnixNano()/1000000, 10)
    p := []byte(t + plain)
    s := string(Sign(p, []byte(ks.apiSecret)))
    return map[string]string{
        "KC-API-KEY":         ks.apiKey,
        "KC-API-PASSPHRASE":  ks.apiPassPhrase,
        "KC-API-TIMESTAMP":   t,
        "KC-API-SIGN":        s,
        "KC-API-KEY-VERSION": "2",
    }
}
```

---

## Important Notes

1. **Timestamp** must match between header and signature calculation
2. **Body** for signature must exactly match actual request body
3. For **GET/DELETE**: Query params in URL, body = empty string
4. For **POST**: Query params in JSON body
5. Use **original unencoded URL** content for signature (handles special characters)
