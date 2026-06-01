pub mod scraper;
pub mod sentiment;

pub use scraper::{fetch_and_store_news, NewsScraper};
pub use sentiment::SentimentScorer;
