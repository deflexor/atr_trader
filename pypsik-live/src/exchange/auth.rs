/// Bybit V5 API authentication — HMAC-SHA256 signature generation.

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Build the signature string and compute HMAC-SHA256.
///
/// GET: `timestamp + api_key + recv_window + query_string`
/// POST: `timestamp + api_key + recv_window + body`
pub fn sign(params: &str, secret: &str) -> String {
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .expect("HMAC key length is valid");
    mac.update(params.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}
