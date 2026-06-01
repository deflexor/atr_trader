pub mod config;
pub mod engine;
pub mod fills;
pub mod funding_arb_engine;
pub mod metrics;

pub use config::BacktestConfig;
pub use engine::{BacktestEngine, BacktestResult, EquityPoint, TradeRecord};
pub use fills::FillSimulator;
pub use metrics::PerformanceMetrics;
