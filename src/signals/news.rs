//! News sentiment signal — aggregates recent news sentiment into a sub-signal.
//!
//! Sentiment is additive: it boosts/cuts strength but never forces a direction
//! when tech signals are neutral.
//!
//! For backtesting, [`NewsSignalCache`] pre-loads news into a sync lookup table.

use std::collections::HashMap;

use crate::data::DataStore;
use crate::error::Result;
use crate::signals::tech::SubSignal;
use crate::types::news::NewsArticle;
use crate::types::signal::SignalDirection;

/// A synchronous cache of news articles for backtesting.
///
/// Pre-loads all articles within a date range, indexed by timestamp bucket.
/// Allows the sync signal generator to factor in news sentiment without DB access.
#[derive(Clone)]
pub struct NewsSignalCache {
    /// Articles indexed by (coin_key, timestamp_bucket).
    /// Bucket = published_at / bucket_size_secs (e.g., 1h = 3600)
    articles: HashMap<(String, i64), Vec<f64>>,
    /// How many seconds each bucket covers
    bucket_size: i64,
    /// How many buckets to look back from current candle
    lookback_buckets: i64,
}

impl NewsSignalCache {
    /// Build a cache from all articles in the database within [from_ts, to_ts].
    ///
    /// `bucket_secs`: granularity for time bucketing (default: 3600 = 1h).
    /// `lookback_hours`: how far back from each candle to consider news.
    pub async fn build(
        store: &DataStore,
        from_ts: i64,
        to_ts: i64,
        bucket_secs: i64,
        lookback_hours: u64,
    ) -> Result<Self> {
        // Fetch all articles in the range
        let articles = store
            .get_news_for_coins(&[], from_ts, to_ts, 999999)
            .await?;

        let bucket_size = if bucket_secs <= 0 { 3600 } else { bucket_secs };
        let lookback_buckets = (lookback_hours as i64 * 3600) / bucket_size;

        let mut indexed: HashMap<(String, i64), Vec<f64>> = HashMap::new();

        for article in &articles {
            let bucket = article.published_at / bucket_size;
            // Index by each coin mentioned in the article
            if article.coins.is_empty() {
                // Store under generic "_all" for coins we don't track
                indexed
                    .entry(("_all".to_string(), bucket))
                    .or_default()
                    .push(article.sentiment_score);
            } else {
                for coin in &article.coins {
                    indexed
                        .entry((coin.to_uppercase(), bucket))
                        .or_default()
                        .push(article.sentiment_score);
                }
            }
        }

        Ok(Self {
            articles: indexed,
            bucket_size,
            lookback_buckets,
        })
    }

    /// Look up aggregate sentiment for a coin near a given timestamp.
    ///
    /// Returns (avg_sentiment, article_count) or (0.0, 0) if nothing found.
    pub fn sentiment_at(&self, coin: &str, timestamp: i64) -> (f64, usize) {
        let coin = coin.to_uppercase();
        let current_bucket = timestamp / self.bucket_size;
        let start_bucket = current_bucket - self.lookback_buckets;

        let mut total_score = 0.0_f64;
        let mut total_count = 0_usize;

        for bucket in start_bucket..=current_bucket {
            // Check coin-specific articles
            if let Some(scores) = self.articles.get(&(coin.clone(), bucket)) {
                for &s in scores {
                    total_score += s;
                    total_count += 1;
                }
            }
            // Also check generic articles
            if let Some(scores) = self.articles.get(&("_all".to_string(), bucket)) {
                for &s in scores {
                    total_score += s;
                    total_count += 1;
                }
            }
        }

        if total_count == 0 {
            (0.0, 0)
        } else {
            (total_score / total_count as f64, total_count)
        }
    }

    /// Compute a news sentiment sub-signal from this cache.
    pub fn compute_signal(&self, coin: &str, timestamp: i64) -> SubSignal {
        let (avg_sentiment, count) = self.sentiment_at(coin, timestamp);
        if count == 0 {
            return SubSignal::neutral("news_sentiment");
        }

        if avg_sentiment > 0.2 {
            let strength = (avg_sentiment * 0.3).min(0.3);
            SubSignal {
                direction: SignalDirection::Long,
                strength,
                source: "news_sentiment",
            }
        } else if avg_sentiment < -0.2 {
            let strength = ((-avg_sentiment) * 0.3).min(0.3);
            SubSignal {
                direction: SignalDirection::Short,
                strength,
                source: "news_sentiment",
            }
        } else {
            SubSignal::neutral("news_sentiment")
        }
    }

    /// Number of articles in the cache.
    pub fn article_count(&self) -> usize {
        self.articles.values().map(|v| v.len()).sum()
    }
}

/// Compute a news sentiment sub-signal by aggregating recent articles
/// for the given symbol's coin.
///
/// Returns a SubSignal with:
/// - Direction: LONG if avg sentiment > 0.2, SHORT if < -0.2, else NEUTRAL
/// - Strength: proportional to |avg sentiment|, max 0.3
///
/// Sentiment is intentionally capped so it enhances but never dominates
/// tech signals.
pub async fn compute_news_signal(
    store: &DataStore,
    symbol: &str,
    lookback_hours: u64,
) -> Result<SubSignal> {
    let now = chrono::Utc::now().timestamp();
    let from_ts = now - (lookback_hours as i64 * 3600);

    // Map our symbol to a coin keyword for news lookup
    let coin_key = symbol_to_coin_key(symbol);

    // Fetch recent news that mentions this coin
    let articles = store
        .get_news_for_coins(&[], from_ts, now, 50)
        .await?;

    if articles.is_empty() {
        return Ok(SubSignal::neutral("news_sentiment"));
    }

    // Average sentiment across articles mentioning this coin
    let relevant: Vec<&NewsArticle> = articles
        .iter()
        .filter(|a| a.coins.iter().any(|c| c.eq_ignore_ascii_case(&coin_key)))
        .collect();

    let (scores, count) = if relevant.is_empty() {
        // Fall back to all articles
        let scores: Vec<f64> = articles.iter().map(|a| a.sentiment_score).collect();
        (scores, articles.len())
    } else {
        let scores: Vec<f64> = relevant.iter().map(|a| a.sentiment_score).collect();
        (scores, relevant.len())
    };

    // Average sentiment
    let avg_sentiment: f64 = scores.iter().sum::<f64>() / count as f64;

    // Compute signal from aggregate sentiment
    // Cap strength at 0.3 so news is always an enhancer, never dominant
    if avg_sentiment > 0.2 {
        let strength = (avg_sentiment * 0.3).min(0.3);
        Ok(SubSignal {
            direction: SignalDirection::Long,
            strength,
            source: "news_sentiment",
        })
    } else if avg_sentiment < -0.2 {
        let strength = ((-avg_sentiment) * 0.3).min(0.3);
        Ok(SubSignal {
            direction: SignalDirection::Short,
            strength,
            source: "news_sentiment",
        })
    } else {
        Ok(SubSignal::neutral("news_sentiment"))
    }
}

/// Map our trading symbols to coin key names used in news article tags.
fn symbol_to_coin_key(symbol: &str) -> String {
    match symbol {
        "SOLUSDC" => "SOL".to_string(),
        "BTCUSDC" => "BTC".to_string(),
        "ETHUSDC" => "ETH".to_string(),
        s if s.ends_with("USDC") => s[..s.len() - 4].to_string(),
        s if s.ends_with("USDT") => s[..s.len() - 4].to_string(),
        _ => symbol.to_string(),
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_symbol_to_coin_key() {
        assert_eq!(symbol_to_coin_key("SOLUSDC"), "SOL");
        assert_eq!(symbol_to_coin_key("BTCUSDC"), "BTC");
        assert_eq!(symbol_to_coin_key("ETHUSDT"), "ETH");
        assert_eq!(symbol_to_coin_key("DOGEUSDC"), "DOGE");
    }
}
