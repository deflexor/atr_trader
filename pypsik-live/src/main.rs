//! PyPSiK Live Trading Engine — Rust implementation.
//!
//! Usage: pypsik-live [--testnet] [--symbols BTCUSDT,ETHUSDT]

mod exchange;
mod live;
mod models;
mod risk;
mod strategy;

use std::env;

use clap::Parser;
use tokio::signal;
use tokio::sync::broadcast;
use tracing_subscriber::{fmt, EnvFilter};

use live::trader::{LiveTrader, LiveTradingConfig};

/// PyPSiK live trading (USDT perpetuals)
#[derive(Parser, Debug)]
#[command(name = "pypsik-live", version, about)]
struct Cli {
    /// Use Bybit testnet
    #[arg(long)]
    testnet: bool,

    /// Comma-separated symbols (default from config)
    #[arg(long)]
    symbols: Option<String>,

    /// Initial capital
    #[arg(long, default_value_t = 100.0)]
    capital: f64,

    /// Risk per trade (fraction)
    #[arg(long, default_value_t = 0.03)]
    risk: f64,

    /// Max positions per symbol
    #[arg(long, default_value_t = 2)]
    max_positions: u32,

    /// Leverage (default: 1 = no leverage)
    #[arg(long, default_value_t = 1)]
    leverage: u32,

    /// Market type: perp or spot
    #[arg(long, default_value = "perp")]
    market_type: String,
}

#[tokio::main]
async fn main() {
    // Initialize tracing
    fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .json()
        .init();

    let cli = Cli::parse();

    let api_key = env::var("BYBIT_API_KEY").unwrap_or_default();
    let api_secret = env::var("BYBIT_API_SECRET").unwrap_or_default();

    if api_key.is_empty() || api_secret.is_empty() {
        eprintln!("ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set");
        std::process::exit(1);
    }

    let symbols: Vec<String> = cli
        .symbols
        .map(|s| {
            s.split(',')
                .map(|sym| sym.trim().to_uppercase())
                .collect()
        })
        .unwrap_or_else(|| {
            vec![
                "BTCUSDT".into(),
                "ETHUSDT".into(),
                "DOGEUSDT".into(),
                "TRXUSDT".into(),
                "SOLUSDT".into(),
                "ADAUSDT".into(),
                "AVAXUSDT".into(),
                "UNIUSDT".into(),
            ]
        });

    let config = LiveTradingConfig {
        api_key,
        api_secret,
        testnet: cli.testnet,
        market_type: cli.market_type,
        leverage: cli.leverage,
        symbols,
        initial_capital: cli.capital,
        risk_per_trade: cli.risk,
        max_positions: cli.max_positions,
        ..Default::default()
    };

    tracing::info!(
        symbols = ?config.symbols,
        capital = config.initial_capital,
        risk = config.risk_per_trade,
        max_positions = config.max_positions,
        market_type = %config.market_type,
        leverage = config.leverage,
        testnet = config.testnet,
        "live_trading.starting"
    );

    let (shutdown_tx, shutdown_rx) = broadcast::channel::<()>(1);

    // Spawn Ctrl+C handler
    tokio::spawn(async move {
        signal::ctrl_c()
            .await
            .expect("failed to listen for ctrl+c");
        let _ = shutdown_tx.send(());
        tracing::info!("ctrl_c_received");
    });

    let mut trader = LiveTrader::new(config);

    match trader.start().await {
        Ok(()) => {
            trader.run(shutdown_rx).await;
        }
        Err(e) => {
            tracing::error!(error = %e, "trader_crashed");
        }
    }

    trader.stop().await;
}
