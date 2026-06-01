use thiserror::Error;

#[derive(Debug, Error)]
pub enum TraderError {
    #[error("Storage error: {0}")]
    Storage(#[from] sqlx::Error),

    #[error("API error: {0}")]
    Api(String),

    #[error("Config error: {0}")]
    Config(String),

    #[error("Network error: {0}")]
    Network(#[from] reqwest::Error),

    #[error("Invalid state: {0}")]
    State(String),

    #[error("Exchange error: {0}")]
    Exchange(String),

    #[error("Not found: {0}")]
    NotFound(String),
}

pub type Result<T> = std::result::Result<T, TraderError>;

impl From<std::num::ParseFloatError> for TraderError {
    fn from(e: std::num::ParseFloatError) -> Self {
        TraderError::Api(format!("Float parse error: {e}"))
    }
}
