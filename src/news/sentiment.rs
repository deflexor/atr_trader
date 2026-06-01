//! Rule-based sentiment analyzer for crypto news headlines.
//!
//! Uses a VADER-inspired keyword scoring approach:
//! - Each word is scored for sentiment intensity
//! - Amplifiers boost adjacent word scores
//! - Negators flip the sign of adjacent words
//! - Final score is normalized to [-1.0, 1.0]

use std::collections::HashMap;

/// A scoring entry for a sentiment word.
#[derive(Debug, Clone)]
struct SentimentEntry {
    score: f64,
    is_amplifier: bool,
    is_negator: bool,
}

/// Rule-based sentiment scorer.
///
/// Thread-safe (immutable after construction) — create once, reuse.
pub struct SentimentScorer {
    /// Word → score mapping
    words: HashMap<String, SentimentEntry>,
    /// Coin symbol → canonical ticker
    coins: HashMap<String, String>,
}

impl SentimentScorer {
    /// Build the scorer with default crypto keyword dictionary.
    pub fn new() -> Self {
        let mut words = HashMap::new();

        // ── Positive words ──
        for (w, s) in &[
            ("moon", 0.6),
            ("pump", 0.5),
            ("bullish", 0.7),
            ("breakout", 0.5),
            ("adoption", 0.4),
            ("partnership", 0.5),
            ("upgrade", 0.3),
            ("launch", 0.3),
            ("approval", 0.5),
            ("positive", 0.4),
            ("surge", 0.5),
            ("rally", 0.5),
            ("gain", 0.3),
            ("gains", 0.4),
            ("green", 0.2),
            ("ath", 0.6),
            ("growth", 0.3),
            ("innovation", 0.3),
            ("soar", 0.5),
            ("soars", 0.5),
            ("skyrocket", 0.6),
            ("skyrockets", 0.6),
            ("surge", 0.5),
            ("surges", 0.5),
            ("buy", 0.3),
            ("buying", 0.3),
            ("boom", 0.5),
            ("booming", 0.5),
            ("profit", 0.4),
            ("profitable", 0.5),
            ("upgrade", 0.3),
            ("upgrades", 0.3),
            ("success", 0.4),
            ("successful", 0.4),
            ("opportunity", 0.3),
            ("breakthrough", 0.5),
            ("mainnet", 0.2),
            ("scaling", 0.2),
            ("ecosystem", 0.2),
            ("institutional", 0.3),
            ("inflows", 0.4),
            ("accumulation", 0.3),
            ("hodl", 0.2),
            ("rebound", 0.4),
            ("rebounds", 0.4),
            ("recovery", 0.4),
            ("recover", 0.3),
            ("outperform", 0.5),
            ("outperformance", 0.5),
            ("dividend", 0.3),
            ("yield", 0.2),
            ("staking", 0.2),
            ("defi", 0.1),
            ("nft", 0.1),
            ("metaverse", 0.1),
            ("web3", 0.1),
        ] {
            words.insert(
                w.to_string(),
                SentimentEntry {
                    score: *s,
                    is_amplifier: false,
                    is_negator: false,
                },
            );
        }

        // ── Negative words ──
        for (w, s) in &[
            ("crash", -0.7),
            ("crashes", -0.7),
            ("crashed", -0.7),
            ("dump", -0.6),
            ("dumped", -0.6),
            ("dumping", -0.6),
            ("bearish", -0.7),
            ("ban", -0.6),
            ("banned", -0.6),
            ("hack", -0.8),
            ("hacked", -0.8),
            ("hacking", -0.8),
            ("scam", -0.9),
            ("fraud", -0.9),
            ("loss", -0.4),
            ("losses", -0.5),
            ("decline", -0.4),
            ("declines", -0.4),
            ("declined", -0.4),
            ("drop", -0.4),
            ("drops", -0.4),
            ("dropped", -0.4),
            ("fall", -0.3),
            ("falls", -0.3),
            ("fell", -0.3),
            ("negative", -0.4),
            ("fud", -0.5),
            ("selloff", -0.5),
            ("sell-off", -0.5),
            ("correction", -0.3),
            ("regulation", -0.3),
            ("crackdown", -0.6),
            ("exploit", -0.7),
            ("exploited", -0.7),
            ("liquidation", -0.5),
            ("liquidations", -0.5),
            ("bankrupt", -0.7),
            ("bankruptcy", -0.8),
            ("rug", -0.8),
            ("rugpull", -0.9),
            ("sell", -0.2),
            ("selling", -0.2),
            ("fear", -0.4),
            ("panic", -0.5),
            ("panic", -0.5),
            ("worry", -0.3),
            ("worries", -0.3),
            ("risk", -0.2),
            ("risky", -0.3),
            ("danger", -0.4),
            ("dangerous", -0.4),
            ("warning", -0.3),
            ("warn", -0.3),
            ("threat", -0.4),
            ("outflows", -0.4),
            ("capitulation", -0.5),
            ("bear", -0.5),
            ("bear market", -0.6),
            ("recession", -0.5),
            ("inflation", -0.3),
            ("tax", -0.2),
            ("taxes", -0.2),
            ("fine", -0.3),
            ("fines", -0.3),
            ("fined", -0.3),
            ("arrest", -0.5),
            ("lawsuit", -0.4),
            ("investigation", -0.3),
            ("probe", -0.2),
            ("delist", -0.5),
            ("delisted", -0.5),
            ("suspend", -0.3),
            ("suspension", -0.3),
            ("withdrawal", -0.2),
            ("withdrawals", -0.2),
            ("bug", -0.4),
            ("vulnerability", -0.5),
            ("attack", -0.6),
            ("malware", -0.7),
            ("phishing", -0.6),
        ] {
            words.insert(
                w.to_string(),
                SentimentEntry {
                    score: *s,
                    is_amplifier: false,
                    is_negator: false,
                },
            );
        }

        // ── Amplifiers ──
        for w in &["major", "huge", "significant", "massive", "severe", "extreme", "enormous", "tremendous"] {
            words.insert(
                w.to_string(),
                SentimentEntry {
                    score: 0.0,
                    is_amplifier: true,
                    is_negator: false,
                },
            );
        }

        // ── Negators ──
        for w in &["not", "no", "neither", "never", "nobody", "nothing", "nowhere"] {
            words.insert(
                w.to_string(),
                SentimentEntry {
                    score: 0.0,
                    is_amplifier: false,
                    is_negator: true,
                },
            );
        }

        // ── Coin mentions for extraction ──
        let coins: HashMap<String, String> = [
            ("bitcoin", "BTC"),
            ("btc", "BTC"),
            ("ethereum", "ETH"),
            ("eth", "ETH"),
            ("solana", "SOL"),
            ("sol", "SOL"),
            ("ripple", "XRP"),
            ("xrp", "XRP"),
            ("cardano", "ADA"),
            ("ada", "ADA"),
            ("dogecoin", "DOGE"),
            ("doge", "DOGE"),
            ("polkadot", "DOT"),
            ("dot", "DOT"),
            ("avalanche", "AVAX"),
            ("avax", "AVAX"),
            ("polygon", "MATIC"),
            ("matic", "MATIC"),
            ("chainlink", "LINK"),
            ("link", "LINK"),
            ("litecoin", "LTC"),
            ("ltc", "LTC"),
            ("uniswap", "UNI"),
            ("uni", "UNI"),
            ("stellar", "XLM"),
            ("xlm", "XLM"),
            ("cosmos", "ATOM"),
            ("atom", "ATOM"),
            ("monero", "XMR"),
            ("xmr", "XMR"),
            ("tron", "TRX"),
            ("trx", "TRX"),
            ("eos", "EOS"),
            ("tezos", "XTZ"),
            ("xtz", "XTZ"),
            ("filecoin", "FIL"),
            ("fil", "FIL"),
            ("aptos", "APT"),
            ("apt", "APT"),
            ("sui", "SUI"),
            ("arbitrum", "ARB"),
            ("arb", "ARB"),
            ("optimism", "OP"),
            ("op", "OP"),
            ("near", "NEAR"),
            ("algorand", "ALGO"),
            ("algo", "ALGO"),
            ("vechain", "VET"),
            ("vet", "VET"),
            ("theta", "THETA"),
            ("aave", "AAVE"),
            ("maker", "MKR"),
            ("mkr", "MKR"),
            ("compound", "COMP"),
            ("comp", "COMP"),
            ("curve", "CRV"),
            ("crv", "CRV"),
            ("synthetix", "SNX"),
            ("snx", "SNX"),
            ("bittorrent", "BTT"),
            ("btt", "BTT"),
            ("helium", "HNT"),
            ("hnt", "HNT"),
            ("gala", "GALA"),
            ("decentraland", "MANA"),
            ("mana", "MANA"),
            ("sandbox", "SAND"),
            ("sand", "SAND"),
            ("axie", "AXS"),
            ("axs", "AXS"),
            ("immutable", "IMX"),
            ("imx", "IMX"),
            ("flow", "FLOW"),
            ("injective", "INJ"),
            ("inj", "INJ"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();

        Self { words, coins }
    }

    /// Score a title and return (sentiment_score, label).
    ///
    /// Score is in [-1.0, 1.0]. Labels:
    /// - > 0.2 → "bullish"
    /// - < -0.2 → "bearish"
    /// - else → "neutral"
    pub fn score(&self, title: &str) -> (f64, String) {
        let score = self.compute_score(title);
        let label = if score > 0.2 {
            "bullish"
        } else if score < -0.2 {
            "bearish"
        } else {
            "neutral"
        };
        (score, label.to_string())
    }

    /// Compute the raw sentiment score for a title.
    fn compute_score(&self, title: &str) -> f64 {
        let lower = title.to_lowercase();
        let tokens: Vec<&str> = lower
            .split(|c: char| !c.is_alphanumeric())
            .filter(|t| !t.is_empty())
            .collect();

        if tokens.is_empty() {
            return 0.0;
        }

        let mut total = 0.0;
        let mut count = 0;
        let mut i = 0;

        while i < tokens.len() {
            let token = tokens[i];
            if let Some(entry) = self.words.get(token) {
                if entry.is_amplifier {
                    // Skip amplifier — it boosts the NEXT word
                    if i + 1 < tokens.len() {
                        if let Some(next_entry) = self.words.get(tokens[i + 1]) {
                            if !next_entry.is_amplifier && !next_entry.is_negator {
                                let mut score = next_entry.score * 1.5;
                                // Check if negator precedes this amplifier
                                if i > 0 {
                                    if let Some(prev) = self.words.get(tokens[i - 1]) {
                                        if prev.is_negator {
                                            score = -score;
                                        }
                                    }
                                }
                                total += score;
                                count += 1;
                                i += 2; // skip next token
                                continue;
                            }
                        }
                    }
                    // No word after to amplify, skip
                    i += 1;
                    continue;
                }

                if entry.is_negator {
                    // Negator flips the NEXT sentiment word
                    if i + 1 < tokens.len() {
                        if let Some(next_entry) = self.words.get(tokens[i + 1]) {
                            if !next_entry.is_amplifier && !next_entry.is_negator {
                                total -= next_entry.score; // flip sign
                                count += 1;
                                i += 2;
                                continue;
                            }
                        }
                    }
                    // Negator with no following word — just skip
                    i += 1;
                    continue;
                }

                // Regular sentiment word
                total += entry.score;
                count += 1;
            }
            i += 1;
        }

        if count == 0 {
            return 0.0;
        }

        // Normalize: divide by count, clamp to [-1.0, 1.0]
        let normalized = total / count as f64;
        normalized.clamp(-1.0, 1.0)
    }

    /// Extract mentioned coins from a title.
    pub fn extract_coins(&self, title: &str) -> Vec<String> {
        let lower = title.to_lowercase();
        let tokens: Vec<&str> = lower
            .split(|c: char| !c.is_alphanumeric())
            .filter(|t| !t.is_empty())
            .collect();

        let mut found = Vec::new();
        let mut seen = std::collections::HashSet::new();

        for token in &tokens {
            if let Some(ticker) = self.coins.get(*token) {
                if seen.insert(ticker.clone()) {
                    found.push(ticker.clone());
                }
            }
        }

        found
    }

    /// Get the number of known words (for diagnostics).
    pub fn word_count(&self) -> usize {
        self.words.len()
    }

    /// Get the number of known coins (for diagnostics).
    pub fn coin_count(&self) -> usize {
        self.coins.len()
    }
}

impl Default for SentimentScorer {
    fn default() -> Self {
        Self::new()
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sentiment_bullish() {
        let scorer = SentimentScorer::new();
        let (score, label) = scorer.score("Bitcoin moon breakout!");
        assert!(score > 0.3, "Expected bullish score > 0.3, got {score}");
        assert_eq!(label, "bullish");
    }

    #[test]
    fn test_sentiment_bearish() {
        let scorer = SentimentScorer::new();
        let (score, label) = scorer.score("Crypto crash hack exploit scam");
        assert!(score < -0.3, "Expected bearish score < -0.3, got {score}");
        assert_eq!(label, "bearish");
    }

    #[test]
    fn test_sentiment_mixed() {
        let scorer = SentimentScorer::new();
        // Contains both positive and negative
        let (_, label) = scorer.score("Bitcoin surges then crashes");
        assert_eq!(label, "neutral");
    }

    #[test]
    fn test_sentiment_empty() {
        let scorer = SentimentScorer::new();
        let (score, label) = scorer.score("BTC at 50000");
        assert_eq!(score, 0.0);
        assert_eq!(label, "neutral");
    }

    #[test]
    fn test_negator_flips() {
        let scorer = SentimentScorer::new();
        // "not bullish" should be negative
        let (score, _) = scorer.score("Not bullish on Bitcoin");
        assert!(score < 0.0, "Expected negative score, got {score}");
    }

    #[test]
    fn test_amplifier_boosts() {
        let scorer = SentimentScorer::new();
        // "massive" should amplify "breakout"
        let (with, _) = scorer.score("Massive breakout");
        let (without, _) = scorer.score("Breakout");
        assert!(
            with.abs() > without.abs(),
            "Amplifier should boost score: with={with}, without={without}"
        );
    }

    #[test]
    fn test_coin_extraction() {
        let scorer = SentimentScorer::new();
        let coins = scorer.extract_coins("BTC and SOL pumped together");
        assert_eq!(coins, vec!["BTC", "SOL"]);
    }

    #[test]
    fn test_coin_extraction_no_duplicates() {
        let scorer = SentimentScorer::new();
        let coins = scorer.extract_coins("BTC bitcoin Bitcoin btc");
        assert_eq!(coins, vec!["BTC"]);
    }

    #[test]
    fn test_coin_extraction_empty() {
        let scorer = SentimentScorer::new();
        let coins = scorer.extract_coins("Weather is nice today");
        assert!(coins.is_empty());
    }

    #[test]
    fn test_word_count() {
        let scorer = SentimentScorer::new();
        assert!(scorer.word_count() > 50);
    }

    #[test]
    fn test_coin_count() {
        let scorer = SentimentScorer::new();
        assert!(scorer.coin_count() > 30);
    }
}
