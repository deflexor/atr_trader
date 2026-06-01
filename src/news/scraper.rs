//! News scrapers for multiple free sources.
//!
//! Each scraper implements [`NewsScraper`] and can fetch the latest articles.
//! The [`fetch_and_store_news`] function orchestrates fetching, scoring, and storing.

use async_trait::async_trait;
use reqwest::Client;
use sha2::{Digest, Sha256};

/// Default HTTP timeout for news feed requests.
/// Some feeds (Cointelegraph, Decrypt) are very slow.
const HTTP_TIMEOUT_SECS: u64 = 30;

/// Maximum time to wait for curl subprocess.
const CURL_TIMEOUT_SECS: u64 = 120;

use crate::error::{Result, TraderError};
use crate::news::sentiment::SentimentScorer;
use crate::types::news::NewsArticle;

/// Parse an item from either RSS or Atom feed into a RawArticle.
/// Works with both `rss::Item` and `atom_syndication::Entry`.
fn raw_article_from_rss_item(item: &rss::Item, _source: &str) -> Option<RawArticle> {
    let title = item.title()?;
    let link = item
        .link()
        .or_else(|| item.comments.as_deref())
        .unwrap_or(title);
    let pub_date = item
        .pub_date()
        .and_then(|d| chrono::DateTime::parse_from_rfc2822(d).ok())
        .map(|dt| dt.timestamp());

    Some(RawArticle {
        title: title.to_string(),
        url: link.to_string(),
        published_at: pub_date.unwrap_or_else(|| chrono::Utc::now().timestamp()),
    })
}

fn raw_article_from_atom_entry(entry: &atom_syndication::Entry, _source: &str) -> Option<RawArticle> {
    let title = entry.title();
    let link = entry
        .links()
        .first()
        .map(|l| l.href())
        .unwrap_or(title);
    let pub_date = entry
        .published()
        .or_else(|| Some(entry.updated()))
        .map(|dt| dt.timestamp());

    Some(RawArticle {
        title: title.to_string(),
        url: link.to_string(),
        published_at: pub_date.unwrap_or_else(|| chrono::Utc::now().timestamp()),
    })
}

/// Pre-clean malformed XML before parsing.
///
/// Common issues in RSS feeds:
/// - Broken CDATA sections: `]]>` missing or split
/// - Unclosed attribute values: trailing `"` missing
/// - Incomplete escape sequences
fn xml_preprocess(body: &str) -> String {
    // Fix broken CDATA: ensure every `<![CDATA[` has a matching `]]>`
    // Replace malformed CDATA end markers
    let mut result = body.to_string();
    
    // Some feeds close CDATA with `]] >` or `] ]>` instead of `]]>`
    result = result.replace("]] >", "]]>");
    result = result.replace("] ]>", "]]>");
    
    // Some feeds have CDATA that starts but never ends — close it
    let cdata_open = result.matches("<![CDATA[").count();
    let cdata_close = result.matches("]]>").count();
    if cdata_open > cdata_close {
        result.push_str("]]>");
    }
    
    // Fix unclosed attribute values (missing trailing quote at end of file)
    // This only handles the case where the last attribute is truncated
    if !result.trim_end().ends_with('"') && !result.trim_end().ends_with('\'') && !result.trim_end().ends_with('>') {
        // The file might be truncated — append a closing quote to the last line
        let trimmed = result.trim_end();
        if trimmed.ends_with('=') || trimmed.ends_with('"') {
            result.push('"');
        }
    }
    
    result
}

/// Fetch a feed URL using curl subprocess (more reliable for slow connections).
pub async fn fetch_via_curl(url: &str, source: &str) -> Result<String> {
    let output = tokio::process::Command::new("curl")
        .args([
            "-sL",
            "--max-time",
            &CURL_TIMEOUT_SECS.to_string(),
            "-H",
            "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            url,
        ])
        .output()
        .await
        .map_err(|e| TraderError::Api(format!("curl failed for {source}: {e}")))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(TraderError::Api(format!(
            "curl error {} for {source}: {stderr}",
            output.status
        )));
    }

    let body = String::from_utf8_lossy(&output.stdout).to_string();
    if body.is_empty() {
        return Err(TraderError::Api(format!("Empty response from {source}")));
    }

    Ok(body)
}

/// Try to parse body as RSS, then as Atom, with fallback to simple parsing.
/// Parse an RSS/Atom feed body into articles, with fallback for malformed XML.
pub fn parse_feed(body: &str, source: &str) -> Vec<RawArticle> {
    let cleaned = xml_preprocess(body);
    
    // Try strict RSS first
    if let Ok(channel) = cleaned.parse::<rss::Channel>() {
        return channel
            .items()
            .iter()
            .filter_map(|item| raw_article_from_rss_item(item, source))
            .collect();
    }

    // Try strict Atom
    if let Ok(feed) = cleaned.parse::<atom_syndication::Feed>() {
        return feed
            .entries()
            .iter()
            .filter_map(|entry| raw_article_from_atom_entry(entry, source))
            .collect();
    }

    // Fallback: simple item extraction for malformed RSS
    simple_extract_items(&cleaned, source)
}

/// Fallback parser: extract items from malformed RSS XML using simple scanning.
/// Handles the common case where the full RSS parser chokes on broken XML.
fn simple_extract_items(body: &str, source: &str) -> Vec<RawArticle> {
    let mut articles = Vec::new();
    let mut pos = 0;
    let bytes = body.as_bytes();
    
    loop {
        // Find next <item> or <entry> tag
        let item_start = match find_tag_start(bytes, pos, b"<item>", b"<entry>") {
            Some(p) => p,
            None => break,
        };
        
        let tag_close = match find_char(bytes, item_start, b'>') {
            Some(p) => p + 1,
            None => break,
        };
        
        // Find closing tag
        let item_end_pos = match find_tag_end(bytes, tag_close, b"</item>", b"</entry>", b"/>" ) {
            Some(end) => end,
            None => break,
        };
        let item_content = &body[tag_close..item_end_pos];
        
        // Extract title
        let title = extract_tag_content(item_content, b"<title>", b"</title>")
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        
        // Extract link
        let link = extract_tag_content(item_content, b"<link>", b"</link>")
            .or_else(|| extract_attr_content(item_content, b"<link", b"href=\"", b"\""))
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        
        // Extract pubDate
        let pub_date = extract_tag_content(item_content, b"<pubDate>", b"</pubDate>")
            .or_else(|| extract_tag_content(item_content, b"<published>", b"</published>"))
            .or_else(|| extract_tag_content(item_content, b"<updated>", b"</updated>"))
            .map(|s| s.trim().to_string());
        
        let ts = pub_date
            .and_then(|d| {
                chrono::DateTime::parse_from_rfc2822(&d)
                    .ok()
                    .map(|dt| dt.timestamp())
                    .or_else(|| {
                        chrono::DateTime::parse_from_rfc3339(&d)
                            .ok()
                            .map(|dt| dt.timestamp())
                    })
                    .or_else(|| {
                        // Try naive datetime
                        chrono::NaiveDateTime::parse_from_str(&d, "%Y-%m-%dT%H:%M:%S")
                            .ok()
                            .map(|dt| dt.and_utc().timestamp())
                    })
            })
            .unwrap_or_else(|| chrono::Utc::now().timestamp());
        
        if title.len() >= 10 && !link.is_empty() {
            articles.push(RawArticle {
                title: html_unescape(&title),
                url: link,
                published_at: ts,
            });
        }
        
        pos = item_end_pos;
    }
    
    articles
}

// ─── Simple XML Scanning Helpers ────────────────────────────

fn find_tag_start(bytes: &[u8], from: usize, tag1: &[u8], tag2: &[u8]) -> Option<usize> {
    let p1 = bytes[from..].windows(tag1.len()).position(|w| w == tag1);
    let p2 = bytes[from..].windows(tag2.len()).position(|w| w == tag2);
    match (p1, p2) {
        (Some(a), Some(b)) => Some(from + a.min(b)),
        (Some(a), None) => Some(from + a),
        (None, Some(b)) => Some(from + b),
        (None, None) => None,
    }
}

fn find_tag_end(bytes: &[u8], from: usize, tag1: &[u8], tag2: &[u8], self_close: &[u8]) -> Option<usize> {
    let mut pos = from;
    loop {
        if pos >= bytes.len() {
            return None;
        }
        // Check self-closing
        if pos + self_close.len() <= bytes.len() && &bytes[pos..pos + self_close.len()] == self_close {
            return Some(pos + self_close.len());
        }
        if pos + tag1.len() <= bytes.len() && &bytes[pos..pos + tag1.len()] == tag1 {
            return Some(pos);
        }
        if pos + tag2.len() <= bytes.len() && &bytes[pos..pos + tag2.len()] == tag2 {
            return Some(pos);
        }
        pos += 1;
    }
}

fn find_char(bytes: &[u8], from: usize, c: u8) -> Option<usize> {
    bytes[from..].iter().position(|&b| b == c).map(|p| from + p)
}

fn extract_tag_content<'a>(content: &'a str, open: &[u8], close: &[u8]) -> Option<String> {
    let bytes = content.as_bytes();
    let start = bytes.windows(open.len()).position(|w| w == open)?;
    let content_start = start + open.len();
    let end = bytes[content_start..].windows(close.len()).position(|w| w == close)?;
    let mut raw = String::from_utf8_lossy(&bytes[content_start..content_start + end]).to_string();
    // Strip CDATA wrapper if present
    raw = raw.trim().to_string();
    if raw.starts_with("<![CDATA[") && raw.ends_with("]]>") {
        raw = raw[9..raw.len()-3].to_string();
    }
    Some(raw)
}

fn extract_attr_content<'a>(content: &'a str, tag: &[u8], prefix: &[u8], suffix: &[u8]) -> Option<String> {
    let bytes = content.as_bytes();
    let tag_pos = bytes.windows(tag.len()).position(|w| w == tag)?;
    let after_tag = &bytes[tag_pos..];
    let attr_start = after_tag.windows(prefix.len()).position(|w| w == prefix)? + prefix.len();
    let after_prefix = &after_tag[attr_start..];
    let attr_end = after_prefix.windows(suffix.len()).position(|w| w == suffix)?;
    Some(String::from_utf8_lossy(&after_prefix[..attr_end]).to_string())
}

/// Simple HTML entity unescape for common entities.
fn html_unescape(s: &str) -> String {
    s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#039;", "'")
        .replace("&#x27;", "'")
        .replace("&#x2F;", "/")
        .replace("&#x60;", "`")
        .replace("&#x3A;", ":")
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
        .replace("&apos;", "'")
}

// ─── URL Hashing ────────────────────────────────────────────

/// Create a SHA256 hex hash of a URL for dedup.
pub fn hash_url(url: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(url.as_bytes());
    hex::encode(hasher.finalize())
}

// ─── Provider Trait ─────────────────────────────────────────

#[async_trait]
pub trait NewsScraper: Send + Sync {
    /// Fetch latest articles. Returns raw (title, url, published_at) tuples
    /// before sentiment scoring.
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>>;

    /// Human-readable source name.
    fn name(&self) -> &'static str;
}

/// Raw article before sentiment scoring.
#[derive(Debug, Clone)]
pub struct RawArticle {
    pub title: String,
    pub url: String,
    pub published_at: i64,
}

// ─── CryptoPanic Scraper ────────────────────────────────────

/// CryptoPanic API scraper.
///
/// Free tier: ~50 requests/day, ~100 posts/request.
/// Requires API key via `cryptopanic_api_key` config or `ATR_CRYPTOPANIC_KEY` env var.
///
/// The API provides its own sentiment via upvote/downvote ratios,
/// which we use as the sentiment score for CryptoPanic articles.
pub struct CryptoPanicScraper {
    http: Client,
    api_key: String,
}

impl CryptoPanicScraper {
    pub fn new(api_key: &str) -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
            api_key: api_key.to_string(),
        }
    }

    /// Fetch articles from CryptoPanic API and parse them into RawArticle.
    async fn fetch_page(&self, page: u32) -> Result<Vec<RawArticle>> {
        let url = format!(
            "https://cryptopanic.com/api/v1/posts/?auth_token={}&kind=news&public=true&page={}",
            self.api_key, page
        );

        let resp = self.http.get(&url).send().await?;

        if !resp.status().is_success() {
            return Err(TraderError::Api(format!(
                "CryptoPanic API error ({}): {}",
                resp.status(),
                resp.text().await.unwrap_or_default()
            )));
        }

        // Parse response
        #[derive(serde::Deserialize)]
        struct CpResponse {
            results: Vec<CpPost>,
        }

        #[derive(serde::Deserialize)]
        struct CpPost {
            title: String,
            url: String,
            published_at: String, // ISO 8601
            domain: Option<String>,
            currencies: Option<Vec<CpCurrency>>,
            votes: CpVotes,
        }

        #[derive(serde::Deserialize)]
        struct CpCurrency {
            code: String,
        }

        #[derive(serde::Deserialize)]
        struct CpVotes {
            positive: i64,
            negative: i64,
            important: Option<i64>,
        }

        let parsed: CpResponse = resp.json().await.map_err(|e| {
            TraderError::Api(format!("CryptoPanic parse error: {e}"))
        })?;

        let articles: Vec<RawArticle> = parsed
            .results
            .into_iter()
            .map(|post| {
                let ts = parse_iso8601_timestamp(&post.published_at).unwrap_or_else(|| {
                    chrono::Utc::now().timestamp()
                });

                RawArticle {
                    title: post.title,
                    url: post.url,
                    published_at: ts,
                }
            })
            .collect();

        Ok(articles)
    }
}

#[async_trait]
impl NewsScraper for CryptoPanicScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        self.fetch_page(1).await
    }

    fn name(&self) -> &'static str {
        "cryptopanic"
    }
}

impl CryptoPanicScraper {
    /// Fetch multiple pages for backfill.
    pub async fn fetch_historical(&self, max_pages: u32) -> Result<Vec<RawArticle>> {
        let mut all = Vec::new();
        for page in 1..=max_pages {
            let articles = self.fetch_page(page).await?;
            let count = articles.len();
            all.extend(articles);
            if count < 100 {
                // Less than 100 means we hit the last page
                break;
            }
            // Rate limit: 1 request per second
            tokio::time::sleep(tokio::time::Duration::from_secs(1)).await;
        }
        Ok(all)
    }
}

// ─── Google News RSS Scraper ────────────────────────────────

/// Google News RSS feed scraper.
///
/// Fetches RSS XML from Google News search results for cryptocurrency.
/// No API key needed. No rate limit concerns.
pub struct GoogleNewsScraper {
    http: Client,
}

impl GoogleNewsScraper {
    pub fn new() -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }
}

impl Default for GoogleNewsScraper {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl NewsScraper for GoogleNewsScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        let url = "https://news.google.com/rss/search?q=cryptocurrency&hl=en-US&gl=US";

        let resp = self.http.get(url).send().await?;

        if !resp.status().is_success() {
            return Err(TraderError::Api(format!(
                "Google News RSS error ({}): {}",
                resp.status(),
                resp.text().await.unwrap_or_default()
            )));
        }

        let body = String::from_utf8_lossy(&resp.bytes().await?).to_string();
        let articles = parse_feed(&body, "googlenews");
        Ok(articles)
    }

    fn name(&self) -> &'static str {
        "googlenews"
    }
}

// ─── Reddit RSS / Atom Scraper ──────────────────────────────

/// Reddit r/CryptoCurrency feed scraper (Atom format).
///
/// Reddit now serves its feeds as Atom XML (not RSS).
/// No API key needed. Fetches ~25 latest posts.
pub struct RedditScraper {
    http: Client,
}

impl RedditScraper {
    pub fn new() -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }
}

impl Default for RedditScraper {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl NewsScraper for RedditScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        let url = "https://www.reddit.com/r/Cryptocurrency/.rss";

        let resp = self.http.get(url).send().await?;

        if !resp.status().is_success() {
            return Err(TraderError::Api(format!(
                "Reddit RSS error ({}): {}",
                resp.status(),
                resp.text().await.unwrap_or_default()
            )));
        }

        let body = String::from_utf8_lossy(&resp.bytes().await?).to_string();
        let articles = parse_feed(&body, "reddit");
        if articles.is_empty() {
            return Err(TraderError::Api(
                "Reddit feed returned no articles (could not parse as RSS or Atom)".to_string(),
            ));
        }

        Ok(articles)
    }

    fn name(&self) -> &'static str {
        "reddit"
    }
}

// ─── Date Parsing Helpers ──────────────────────────────────

/// Parse ISO 8601 timestamp string to unix timestamp.
fn parse_iso8601_timestamp(s: &str) -> Option<i64> {
    // Try RFC 3339 first (CryptoPanic format: "2026-05-30T13:00:00Z")
    if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(s) {
        return Some(dt.timestamp());
    }
    // Try naive datetime (without timezone suffix)
    if let Ok(dt) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S") {
        return Some(dt.and_utc().timestamp());
    }
    None
}

// ─── CoinDesk RSS Scraper ──────────────────────────────────

/// CoinDesk RSS feed scraper.
///
/// Fetches RSS XML from CoinDesk's official feed.
/// No API key needed. Covers Bitcoin, Ethereum, DeFi, regulation news.
pub struct CoinDeskScraper {
    http: Client,
}

impl CoinDeskScraper {
    pub fn new() -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }
}

impl Default for CoinDeskScraper {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl NewsScraper for CoinDeskScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        let url = "https://www.coindesk.com/arc/outboundfeeds/rss/";
        let body = fetch_via_curl(url, "coindesk").await?;
        let articles = parse_feed(&body, "coindesk");
        if articles.is_empty() {
            return Err(TraderError::Api(
                "CoinDesk RSS returned no articles".to_string(),
            ));
        }
        Ok(articles)
    }

    fn name(&self) -> &'static str {
        "coindesk"
    }
}

// ─── CoinTelegraph RSS Scraper ──────────────────────────────

/// CoinTelegraph RSS feed scraper.
///
/// Fetches RSS XML from CoinTelegraph.
/// No API key needed. Covers Bitcoin, Ethereum, altcoins, regulation, market analysis.
pub struct CoinTelegraphScraper {
    http: Client,
}

impl CoinTelegraphScraper {
    pub fn new() -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }
}

impl Default for CoinTelegraphScraper {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl NewsScraper for CoinTelegraphScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        let url = "https://cointelegraph.com/rss";
        let body = fetch_via_curl(url, "cointelegraph").await?;
        let articles = parse_feed(&body, "cointelegraph");
        if articles.is_empty() {
            return Err(TraderError::Api(
                "CoinTelegraph RSS returned no articles".to_string(),
            ));
        }
        Ok(articles)
    }

    fn name(&self) -> &'static str {
        "cointelegraph"
    }
}

// ─── Decrypt RSS Scraper ────────────────────────────────────

/// Decrypt RSS feed scraper.
///
/// Fetches RSS XML from Decrypt.
/// No API key needed. Covers Bitcoin, Ethereum, NFTs, DeFi, gaming, regulatory news.
pub struct DecryptScraper {
    http: Client,
}

impl DecryptScraper {
    pub fn new() -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }
}

impl Default for DecryptScraper {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl NewsScraper for DecryptScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        let url = "https://decrypt.co/feed";
        let body = fetch_via_curl(url, "decrypt").await?;
        let articles = parse_feed(&body, "decrypt");
        if articles.is_empty() {
            return Err(TraderError::Api(
                "Decrypt RSS returned no articles".to_string(),
            ));
        }
        Ok(articles)
    }

    fn name(&self) -> &'static str {
        "decrypt"
    }
}

// ─── U.Today RSS Scraper ────────────────────────────────────

/// U.Today RSS feed scraper.
///
/// Fetches RSS XML from U.Today — popular crypto news outlet.
/// No API key needed.
pub struct UTodayScraper {
    http: Client,
}

impl UTodayScraper {
    pub fn new() -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }
}

impl Default for UTodayScraper {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl NewsScraper for UTodayScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        let url = "https://u.today/rss";
        let body = fetch_via_curl(url, "utoday").await?;
        let articles = parse_feed(&body, "utoday");
        if articles.is_empty() {
            return Err(TraderError::Api(
                "UToday RSS returned no articles".to_string(),
            ));
        }
        Ok(articles)
    }

    fn name(&self) -> &'static str {
        "utoday"
    }
}

// ─── Bitcoin Magazine RSS Scraper ───────────────────────────

/// Bitcoin Magazine RSS feed scraper.
///
/// Fetches RSS XML from Bitcoin Magazine.
/// No API key needed. Bitcoin-focused news and analysis.
pub struct BitcoinMagScraper {
    http: Client,
}

impl BitcoinMagScraper {
    pub fn new() -> Self {
        Self {
            http: Client::builder()
                .no_gzip()
                .no_brotli()
                .no_deflate()
                .user_agent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
                .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }
}

impl Default for BitcoinMagScraper {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl NewsScraper for BitcoinMagScraper {
    async fn fetch_latest(&self) -> Result<Vec<RawArticle>> {
        let url = "https://bitcoinmagazine.com/feed";
        let body = fetch_via_curl(url, "bitcoinmag").await?;
        let articles = parse_feed(&body, "bitcoinmag");
        if articles.is_empty() {
            return Err(TraderError::Api(
                "BitcoinMag RSS returned no articles".to_string(),
            ));
        }
        Ok(articles)
    }

    fn name(&self) -> &'static str {
        "bitcoinmag"
    }
}

// ─── Factory ───────────────────────────────────────────────

/// Create a scraper by source name.
pub fn create_scraper(
    source: &str,
    cryptopanic_api_key: Option<&str>,
) -> Result<Box<dyn NewsScraper>> {
    match source {
        "cryptopanic" => {
            let key = cryptopanic_api_key.ok_or_else(|| {
                TraderError::Config(
                    "CryptoPanic API key required. Set ATR_CRYPTOPANIC_KEY env var or config.cryptopanic_api_key".to_string()
                )
            })?;
            Ok(Box::new(CryptoPanicScraper::new(key)))
        }
        "googlenews" => Ok(Box::new(GoogleNewsScraper::new())),
        "reddit" => Ok(Box::new(RedditScraper::new())),
        "coindesk" => Ok(Box::new(CoinDeskScraper::new())),
        "cointelegraph" => Ok(Box::new(CoinTelegraphScraper::new())),
        "decrypt" => Ok(Box::new(DecryptScraper::new())),
        "utoday" => Ok(Box::new(UTodayScraper::new())),
        "bitcoinmag" => Ok(Box::new(BitcoinMagScraper::new())),
        other => Err(TraderError::Config(format!(
            "Unknown news source '{other}'. Valid: cryptopanic, googlenews, reddit, coindesk, cointelegraph, decrypt, utoday, bitcoinmag"
        ))),
    }
}

// ─── Orchestration ─────────────────────────────────────────

/// Fetch latest news from one or all sources, score with sentiment, store in DB.
///
/// Returns total articles stored.
pub async fn fetch_and_store_news(
    store: &crate::data::DataStore,
    config: &crate::config::Config,
    source_filter: Option<&str>,
) -> Result<usize> {
    let scorer = SentimentScorer::new();
    let api_key = config.news.cryptopanic_api_key.as_deref();
    let mut total_stored = 0;

    let sources: Vec<&str> = if let Some(filter) = source_filter {
        vec![filter]
    } else {
        config.news.sources.iter().map(|s| s.as_str()).collect()
    };

    for source in sources {
        let scraper = match create_scraper(source, api_key) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("Skipping {source}: {e}");
                continue;
            }
        };

        tracing::info!("Fetching news from {source}...");

        let raw = match scraper.fetch_latest().await {
            Ok(articles) => articles,
            Err(e) => {
                tracing::error!("Failed to fetch from {source}: {e}");
                continue;
            }
        };

        tracing::info!("Got {} raw articles from {source}", raw.len());

        // Score each article and convert to NewsArticle
        let mut articles = Vec::new();
        for raw_article in raw {
            // Skip articles with very short titles (noise filter)
            if raw_article.title.len() < 10 {
                continue;
            }

            let url_hash = hash_url(&raw_article.url);
            let coins = scorer.extract_coins(&raw_article.title);
            let (sentiment_score, sentiment_label) = scorer.score(&raw_article.title);
            let now = chrono::Utc::now().timestamp();

            let article = NewsArticle {
                id: None,
                source: source.to_string(),
                url_hash,
                title: raw_article.title,
                coins,
                published_at: raw_article.published_at,
                fetched_at: now,
                sentiment_score,
                sentiment_label,
            };

            articles.push(article);
        }

        // Store
        if !articles.is_empty() {
            let stored = store.insert_news(&articles).await?;
            let skipped = articles.len() - stored as usize;
            total_stored += stored as usize;

            if skipped > 0 {
                tracing::info!(
                    "  Stored {stored} articles from {source} ({skipped} skipped by dedup)"
                );
            } else {
                tracing::info!("  Stored {stored} articles from {source}");
            }
        }
    }

    Ok(total_stored)
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hash_url() {
        let h1 = hash_url("https://example.com/article1");
        let h2 = hash_url("https://example.com/article1");
        let h3 = hash_url("https://example.com/article2");

        assert_eq!(h1, h2, "Same URL should have same hash");
        assert_ne!(h1, h3, "Different URLs should have different hashes");
        assert_eq!(h1.len(), 64, "SHA256 hex should be 64 chars");
    }

    #[test]
    fn test_parse_iso8601() {
        let ts = parse_iso8601_timestamp("2026-05-30T13:00:00Z");
        assert!(ts.is_some(), "Should parse ISO 8601");
        // 2026-05-30 13:00:00 UTC
        assert!(ts.unwrap() > 0);
    }

    #[test]
    fn test_parse_rfc2822() {
        let ts = chrono::DateTime::parse_from_rfc2822("Sat, 30 May 2026 13:00:00 GMT");
        assert!(ts.is_ok(), "Should parse RFC 2822");
        assert!(ts.unwrap().timestamp() > 0);
    }
}


