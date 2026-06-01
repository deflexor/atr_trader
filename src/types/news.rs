use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewsArticle {
    pub id: Option<i64>,
    pub source: String,
    pub url_hash: String,
    pub title: String,
    pub coins: Vec<String>,
    pub published_at: i64,
    pub fetched_at: i64,
    pub sentiment_score: f64,
    pub sentiment_label: String,
}

impl NewsArticle {
    pub fn new(
        source: &str,
        url_hash: &str,
        title: &str,
        coins: Vec<String>,
        published_at: i64,
        sentiment_score: f64,
    ) -> Self {
        let label = if sentiment_score > 0.2 {
            "bullish"
        } else if sentiment_score < -0.2 {
            "bearish"
        } else {
            "neutral"
        };

        Self {
            id: None,
            source: source.to_string(),
            url_hash: url_hash.to_string(),
            title: title.to_string(),
            coins,
            published_at,
            fetched_at: chrono::Utc::now().timestamp(),
            sentiment_score,
            sentiment_label: label.to_string(),
        }
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_news_sentiment_labels() {
        let bullish = NewsArticle::new("test", "hash1", "Bitcoin moon!", vec!["BTC".into()], 1000, 0.8);
        assert_eq!(bullish.sentiment_label, "bullish");

        let bearish = NewsArticle::new("test", "hash2", "Crypto crash", vec!["BTC".into()], 1000, -0.8);
        assert_eq!(bearish.sentiment_label, "bearish");

        let neutral = NewsArticle::new("test", "hash3", "BTC at 50k", vec!["BTC".into()], 1000, 0.0);
        assert_eq!(neutral.sentiment_label, "neutral");
    }

    #[test]
    fn test_news_serde() {
        let a = NewsArticle::new("cryptopanic", "abc123", "Test headline", vec!["SOL".into(), "BTC".into()], 1000, 0.5);
        let json = serde_json::to_string(&a).unwrap();
        let back: NewsArticle = serde_json::from_str(&json).unwrap();
        assert_eq!(a.url_hash, back.url_hash);
        assert_eq!(a.coins.len(), 2);
        assert_eq!(a.sentiment_score, back.sentiment_score);
    }
}
