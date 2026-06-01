use std::path::PathBuf;

use clap::{Parser, Subcommand};
use tracing_subscriber::EnvFilter;

use atr_trader_v2::config::Config;
use atr_trader_v2::data::DataStore;

#[derive(Parser)]
#[command(name = "atr-trader-v2", about = "DEX crypto trading bot with news sentiment", version)]
struct Cli {
    #[arg(short, long, default_value = "./config.toml")]
    config: PathBuf,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Download historical candle data from exchange APIs
    FetchCandles {
        #[arg(short, long)]
        symbol: Option<String>,

        #[arg(short, long, default_value = "365")]
        days: u64,

        /// Data source: auto, coingecko, or binance
        #[arg(long, default_value = "auto")]
        source: String,
    },

    /// Fetch recent news from configured sources
    FetchNews {
        #[arg(short, long)]
        source: Option<String>,
    },

    /// Backfill ~2 years of historical news for backtesting
    BackfillNews,

    /// Run backtest with config
    Backtest {
        #[arg(short, long)]
        symbol: Option<String>,

        #[arg(long, default_value = "false")]
        sweep: bool,

        /// Print detailed trade debugging info
        #[arg(long, default_value = "false")]
        debug: bool,
    },

    /// Start live trading loop
    Live {
        #[arg(long, default_value = "false")]
        dry_run: bool,
    },

    /// Run funding rate arbitrage: backtest or live
    FundingArb {
        /// Run live trading mode
        #[arg(long, default_value = "false")]
        live: bool,

        /// Dry run (no real orders) in live mode
        #[arg(long, default_value = "true")]
        dry_run: bool,

        /// Initial capital for position sizing
        #[arg(long, default_value = "500")]
        capital: f64,

        /// Number of simultaneous carry positions
        #[arg(long, default_value = "3")]
        positions: usize,

        /// Rebalance frequency in days
        #[arg(long, default_value = "14")]
        rebalance: usize,

        /// Transaction cost in bps (5 = maker rebate, 10 = taker)
        #[arg(long, default_value = "5.0")]
        cost_bps: f64,

        /// Minimum funding rate threshold (per 8h, e.g. 0.00005 = 0.005%%)
        #[arg(long, default_value = "0.00005")]
        threshold: f64,

        /// Days of history for backtest (0 = full 4 years)
        #[arg(long, default_value = "0")]
        days: u64,

        /// Exchange for funding data: binance or bybit
        #[arg(long, default_value = "bybit")]
        exchange: String,
    },

    /// Validate configuration without starting
    Validate,

    /// Run diagnostics (data coverage, etc.)
    Check,
}

async fn cmd_fetch_candles(config: &Config, store: &DataStore, symbol: Option<String>, days: u64, source: String) -> anyhow::Result<()> {
    let symbols: Vec<&str> = match &symbol {
        Some(s) => vec![s.as_str()],
        None => config.market_data.symbols.iter().map(|s| s.as_str()).collect(),
    };

    let timeframe = &config.market_data.timeframe;

    for sym in symbols {
        let source_override = if source == "auto" { None } else { Some(source.as_str()) };
        match atr_trader_v2::data::market::fetch_and_store_candles(store, sym, timeframe, days, source_override).await {
            Ok(n) => tracing::info!("  ✓ {sym}: stored {n} candles"),
            Err(e) => tracing::error!("  ✗ {sym}: {e}"),
        }
    }

    Ok(())
}

async fn cmd_fetch_news(config: &Config, store: &DataStore, source: Option<String>) -> anyhow::Result<()> {
    let stored = atr_trader_v2::news::scraper::fetch_and_store_news(
        store,
        config,
        source.as_deref(),
    )
    .await?;

    tracing::info!("Fetched news complete: {stored} new articles stored");
    Ok(())
}

use std::collections::HashMap;
use atr_trader_v2::data::funding::{BinanceFundingProvider, BybitFundingProvider, FundingProvider, funding_rate_annualized, snapshot_funding_rates};
use atr_trader_v2::backtest::funding_arb_engine::backtest_funding_arb;
use atr_trader_v2::strategy::funding_arb::FundingArbConfig;

async fn cmd_funding_arb(
    config: &Config,
    live: bool,
    dry_run: bool,
    capital: f64,
    positions: usize,
    rebalance: usize,
    cost_bps: f64,
    threshold: f64,
    days: u64,
    exchange: &str,
) -> anyhow::Result<()> {
    let symbols = &config.market_data.symbols;

    if live {
        return cmd_funding_arb_live(config, dry_run, capital, positions, rebalance, cost_bps, threshold).await;
    }

    tracing::info!("─── Funding Rate Arb Backtest ───");
    tracing::info!("  Exchange: {}", exchange);
    tracing::info!("  Positions: {}, Rebalance: {}d", positions, rebalance);
    tracing::info!("  Cost: {:.0} bps, Threshold: {:.6}", cost_bps, threshold);

    let provider: Box<dyn FundingProvider> = match exchange {
        "bybit" => Box::new(BybitFundingProvider::new()),
        _ => Box::new(BinanceFundingProvider::new()),
    };

    let fetch_days = if days > 0 { days } else { 1460 }; // default: 4 years

    // Fetch funding rate history
    let mut history: HashMap<String, Vec<(i64, f64)>> = HashMap::new();
    for sym in symbols {
        match provider.fetch_history(sym, fetch_days).await {
            Ok(records) => {
                if records.is_empty() { continue; }
                let rates: Vec<(i64, f64)> = records.iter().map(|r| (r.funding_time, r.rate)).collect();
                let avg = rates.iter().map(|(_, r)| r).sum::<f64>() / rates.len() as f64;
                let above = rates.iter().filter(|(_, r)| *r >= threshold).count();
                tracing::info!("  {:12}: {} records, avg={:.6} ({:.1}% APR), {} above thresh",
                    sym, rates.len(), avg, funding_rate_annualized(avg), above);
                history.insert(sym.to_string(), rates);
            }
            Err(e) => tracing::warn!("  {:12}: {e}", sym),
        }
    }

    if history.is_empty() {
        anyhow::bail!("No funding data fetched!");
    }

    // Build timeline
    let mut all_ts: Vec<i64> = history.values().flat_map(|r| r.iter().map(|(t,_)| *t)).collect();
    all_ts.sort();
    all_ts.dedup();
    let span_days = if all_ts.len() >= 2 {
        (all_ts.last().unwrap() - all_ts.first().unwrap()) / 86400
    } else { 0 };
    tracing::info!("  Timeline: {} timestamps ({} days)", all_ts.len(), span_days);

    // Run backtest
    let fa_config = FundingArbConfig {
        min_rate_threshold: threshold,
        max_positions: positions,
        cost_bps,
        rebalance_days: rebalance,
        ..Default::default()
    };
    let result = backtest_funding_arb(&history, 10000.0, &fa_config);

    tracing::info!("");
    tracing::info!("─── RESULTS ───");
    tracing::info!("  Return:  {:.2}%", result.total_return_pct);
    tracing::info!("  Sharpe:  {:.2}", result.sharpe_ratio);
    tracing::info!("  DD:      {:.2}%", result.max_drawdown_pct);
    tracing::info!("  Carry:   ${:.2}", result.total_carry_collected);
    tracing::info!("  Costs:   ${:.2}", result.total_tx_costs);
    tracing::info!("  Trades:  {}", result.total_trades);
    tracing::info!("  Rebal:   {}", result.rebalances);
    tracing::info!("  Final:   ${:.2}", result.final_capital);

    // Show current funding snapshot from live exchange
    if exchange == "bybit" {
        tracing::info!("");
        tracing::info!("─── Live Bybit Funding Snapshot ───");
        let live = BybitFundingProvider::new();
        let snapshot = snapshot_funding_rates(&live, symbols).await;
        for fr in &snapshot {
            let apr = funding_rate_annualized(fr.rate);
            if fr.rate >= threshold {
                tracing::info!("  ✓ {:12}: {:.6} per 8h ({:.1}% APR)", fr.symbol, fr.rate, apr);
            } else {
                tracing::info!("  ✗ {:12}: {:.6} per 8h ({:.1}% APR)", fr.symbol, fr.rate, apr);
            }
        }
        tracing::info!("  Top picks: {}", snapshot.iter().filter(|r| r.rate >= threshold).take(positions).map(|r| r.symbol.as_str()).collect::<Vec<_>>().join(", "));
    }

    Ok(())
}

/// Launch live funding arb engine on Bybit
async fn cmd_funding_arb_live(
    config: &Config,
    dry_run: bool,
    capital: f64,
    positions: usize,
    rebalance: usize,
    cost_bps: f64,
    threshold: f64,
) -> anyhow::Result<()> {
    use atr_trader_v2::exchange::bybit::BybitClient;
    use atr_trader_v2::live::funding_arb_engine::FundingArbLiveEngine;
    use atr_trader_v2::strategy::funding_arb::FundingArbConfig;

    let symbols = &config.market_data.symbols;
    let fa_config = FundingArbConfig {
        max_positions: positions,
        rebalance_days: rebalance,
        cost_bps,
        min_rate_threshold: threshold,
        ..Default::default()
    };

    let bybit = BybitClient::new(false)
        .map_err(|e| anyhow::anyhow!("Failed to init Bybit client. Set BYBIT_API_KEY and BYBIT_API_SECRET env vars. Error: {e}"))?;

    let mut engine = FundingArbLiveEngine::new(
        bybit,
        fa_config,
        symbols.clone(),
        capital,
        dry_run,
    );

    engine.run().await?;
    Ok(())
}

async fn cmd_backfill_news(config: &Config, store: &DataStore) -> anyhow::Result<()> {
    let api_key = config.news.cryptopanic_api_key.as_deref().ok_or_else(|| {
        anyhow::anyhow!(
            "CryptoPanic API key required. Set ATR_CRYPTOPANIC_KEY env var or configure cryptopanic_api_key in config.toml"
        )
    })?;

    let scraper = atr_trader_v2::news::scraper::CryptoPanicScraper::new(api_key);
    let scorer = atr_trader_v2::news::sentiment::SentimentScorer::new();

    tracing::info!("Backfilling historical news from CryptoPanic (max 10 pages)...");
    let raw = scraper.fetch_historical(10).await?;
    tracing::info!("Got {} raw articles from CryptoPanic", raw.len());

    let mut articles = Vec::new();
    for ra in raw {
        if ra.title.len() < 10 {
            continue;
        }
        let url_hash = atr_trader_v2::news::scraper::hash_url(&ra.url);
        let coins = scorer.extract_coins(&ra.title);
        let (sentiment_score, sentiment_label) = scorer.score(&ra.title);
        let now = chrono::Utc::now().timestamp();

        articles.push(atr_trader_v2::types::news::NewsArticle {
            id: None,
            source: "cryptopanic".to_string(),
            url_hash,
            title: ra.title,
            coins,
            published_at: ra.published_at,
            fetched_at: now,
            sentiment_score,
            sentiment_label,
        });
    }

    let stored = store.insert_news(&articles).await?;
    let skipped = articles.len() - stored as usize;
    tracing::info!("Backfill complete: {stored} stored, {skipped} skipped by dedup");
    Ok(())
}

async fn cmd_backtest(config: &Config, store: &DataStore, symbol: Option<String>, _sweep: bool, debug: bool) -> anyhow::Result<()> {
    use atr_trader_v2::backtest::{BacktestConfig, BacktestEngine};
    use atr_trader_v2::signals::{generate_strategy_signal_sync_with_news, NewsSignalCache, SignalConfig};
    use atr_trader_v2::types::CandleSeries;

    let symbols: Vec<String> = match &symbol {
        Some(s) => vec![s.clone()],
        None => config.market_data.symbols.clone(),
    };

    let signal_cfg: SignalConfig = config.signals.into_signal_config();
    let enabled = config.strategy.enabled_signals.clone();
    let has_news_enabled = enabled.iter().any(|s| s == "news_sentiment");

    for sym in symbols {
        tracing::info!("─── Backtesting {sym} ───");

        // Load candles from DB
        let tf = &config.market_data.timeframe;
        let candles = store.get_candles(&sym, tf, 0, 9999999999, 999999).await?;

        if candles.len() < 100 {
            tracing::warn!("  Only {} candles for {sym} — need at least 100", candles.len());
            continue;
        }

        tracing::info!("  Loaded {} candles", candles.len());

        // Pre-load news cache for the backtest period
        let first_ts = candles.first().map(|c| c.timestamp).unwrap_or(0);
        let last_ts = candles.last().map(|c| c.timestamp).unwrap_or(0);
        let news_cache = if has_news_enabled {
            match NewsSignalCache::build(
                store,
                first_ts - 86400, // 24h before first candle (for lookback)
                last_ts,
                3600,          // 1-hour buckets
                signal_cfg.news_lookback_hours,
            )
            .await
            {
                Ok(cache) => {
                    let count = cache.article_count();
                    tracing::info!("  News cache: {} articles in range", count);
                    Some(cache)
                }
                Err(e) => {
                    tracing::warn!("  Failed to build news cache: {e} — running without news");
                    None
                }
            }
        } else {
            None
        };

        let series = CandleSeries::new(candles.clone());

        // Load 1h data for higher-timeframe bias
        let htf_series = if config.signals.htf_enabled {
            let htf_candles = store.get_candles(&sym, "1h", 0, 9999999999, 999999).await?;
            if htf_candles.len() >= 50 {
                tracing::info!("  Loaded {} 1h candles for HTF bias", htf_candles.len());
                Some(CandleSeries::new(htf_candles))
            } else {
                tracing::warn!("  Not enough 1h data for HTF bias ({} candles)", htf_candles.len());
                None
            }
        } else {
            None
        };

        // Create signal closure for the backtest engine
        let signal_fn_cfg = signal_cfg.clone();
        let signal_fn_enabled = enabled.clone();
        let signal_fn_news_cache = news_cache.clone();
        let signal_fn_htf = htf_series.clone();
        let signal_fn = move |sym: &str, visible: &[atr_trader_v2::types::Candle]| {
            let series = CandleSeries::new(visible.to_vec());
            generate_strategy_signal_sync_with_news(
                &series,
                &signal_fn_cfg,
                &signal_fn_enabled,
                signal_fn_news_cache.as_ref(),
                signal_fn_htf.as_ref(),
            )
        };

        // Configure backtest
        let bt_config = BacktestConfig {
            initial_capital: 10000.0,
            risk_per_trade: config.strategy.risk_per_trade,
            max_positions: config.strategy.max_positions,
            cooldown_candles: config.strategy.cooldown_candles as i64,
            use_trailing_stop: config.strategy.use_trailing_stop,
            min_risk_reward: config.strategy.min_risk_reward,
            stop_loss_pct: config.strategy.stop_loss_pct,
            take_profit_pct: config.strategy.take_profit_pct,
            trailing_activation_atr: config.strategy.trailing_activation_atr,
            trailing_distance_atr: config.strategy.trailing_distance_atr,
            ..Default::default()
        };

        // Count signal directions (scan candles to see what signals would be generated)
        let mut long_count = 0usize;
        let mut short_count = 0usize;
        let mut neutral_count = 0usize;
        let mut news_signals = 0usize;
        for i in 100..series.len() {
            let visible = CandleSeries::new(series.candles[..=i].to_vec());
            let sig = generate_strategy_signal_sync_with_news(
                &visible, &signal_cfg, &enabled, news_cache.as_ref(), htf_series.as_ref(),
            );
            if sig.sources.iter().any(|s| s == "news_sentiment") {
                news_signals += 1;
            }
            match sig.direction {
                atr_trader_v2::types::SignalDirection::Long => long_count += 1,
                atr_trader_v2::types::SignalDirection::Short => short_count += 1,
                atr_trader_v2::types::SignalDirection::Neutral => neutral_count += 1,
            }
        }

        let mut engine = BacktestEngine::new(bt_config);
        let result = engine.run(&series.candles, signal_fn, None);

        // Calculate long vs short trade distribution
        let long_trades = result.trades.iter().filter(|t| t.side == "long").count();
        let short_trades = result.trades.iter().filter(|t| t.side == "short").count();

        // Calculate win rates by side
        let long_wins = result.trades.iter().filter(|t| t.side == "long" && t.pnl.map_or(false, |p| p > 0.0)).count();
        let _short_wins = result.trades.iter().filter(|t| t.side == "short" && t.pnl.map_or(false, |p| p > 0.0)).count();

        // ── Debug: analyze each trade with candle context ──
        if debug {
            tracing::info!("─── Trade Debug ───");
            // Find matching entry for each closed trade
            for (ti, t) in result.trades.iter().enumerate() {
                if t.pnl.is_none() {
                    continue; // skip open/unclosed trades
                }
                // Find the entry trade (with matching side & similar entry_price) to get candle idx
                let entry_ts = result.trades.iter()
                    .filter(|e| e.side == t.side && (e.entry_price - t.entry_price).abs() < 0.01)
                    .filter_map(|e| if e.timestamp > 1000000000 { Some(e.timestamp) } else { None })
                    .next()
                    .unwrap_or(t.timestamp);
                // Find candle at entry timestamp
                let entry_idx = series.candles.iter()
                    .position(|c| c.timestamp >= entry_ts)
                    .unwrap_or(0);
                let entry_candle = &series.candles[entry_idx.min(series.candles.len() - 1)];
                let end_idx = (entry_idx + 11).min(series.candles.len());

                // Find high/low in next 10 candles
                let mut high_after = entry_candle.high;
                let mut low_after = entry_candle.low;
                let mut candle_list = Vec::new();
                for c in series.candles[entry_idx..end_idx].iter() {
                    if c.high > high_after { high_after = c.high; }
                    if c.low < low_after { low_after = c.low; }
                    let dir = if c.close > c.open { "▲" } else { "▼" };
                    candle_list.push(format!("{}{:.2}", dir, c.close));
                }
                let candle_summary = candle_list.chunks(5)
                    .map(|ch| ch.join(" "))
                    .collect::<Vec<_>>()
                    .join(" | ");

                let move_for = if t.side == "long" { high_after - entry_candle.close } else { entry_candle.close - low_after };
                let move_against = if t.side == "long" { entry_candle.close - low_after } else { high_after - entry_candle.close };
                let max_favorable_pct = move_for / entry_candle.close * 100.0;
                let max_adverse_pct = move_against / entry_candle.close * 100.0;

                tracing::info!("  Trade {}: {} entry ${:.2} → exit ${:.2} | PnL: ${:.2} ({:.1}%) | {}",
                    ti + 1, t.side, t.entry_price,
                    t.exit_price.unwrap_or(0.0),
                    t.pnl.unwrap_or(0.0), t.pnl_pct.unwrap_or(0.0),
                    t.reason);
                tracing::info!("    Entry idx={} price=${:.2} | Max fav={:.2}% Max adv={:.2}%", entry_idx, entry_candle.close, max_favorable_pct, max_adverse_pct);
                tracing::info!("    Candles: {}", candle_summary);
            }
        }

        // Print results
        tracing::info!("─── Results for {sym} ───");
        tracing::info!("  Duration: {} ms", result.duration_ms);
        tracing::info!("  Initial capital: ${:.2}", result.initial_capital);
        tracing::info!("  Final capital: ${:.2}", result.final_capital);
        tracing::info!("  Total return: {:.2}%", result.total_return_pct);
        tracing::info!("  Max drawdown: {:.2}%", result.max_drawdown_pct);
        tracing::info!("  Sharpe ratio: {:.2}", result.sharpe_ratio);
        tracing::info!("  Win rate: {:.1}%", result.win_rate);
        tracing::info!("  Total trades: {} (longs: {} / shorts: {})", result.total_trades, long_trades, short_trades);
        tracing::info!("  Winning: {} | Losing: {}", result.winning_trades, result.losing_trades);
        tracing::info!("  Avg win: ${:.2} | Avg loss: ${:.2}", result.avg_win, result.avg_loss);
        tracing::info!("  Profit factor: {:.2}", result.profit_factor);
        tracing::info!("─── Signal Distribution (L/S/N) ───");
        let total_sigs = long_count + short_count + neutral_count;
        if total_sigs > 0 {
            tracing::info!("  LONG: {} ({:.0}%) | SHORT: {} ({:.0}%) | NEUTRAL: {} ({:.0}%)",
                long_count, long_count as f64 / total_sigs as f64 * 100.0,
                short_count, short_count as f64 / total_sigs as f64 * 100.0,
                neutral_count, neutral_count as f64 / total_sigs as f64 * 100.0,
            );
        }
        tracing::info!("  News-influenced signals: {}", news_signals);
        tracing::info!("  Equity points: {}", result.equity_curve.len());

        // Print sample trades
        if !result.trades.is_empty() {
            tracing::info!("─── All closed trades ───");
            let closed: Vec<_> = result.trades.iter().filter(|t| t.pnl.is_some()).collect();
            for t in closed.iter().rev().take(10).rev() {
                let pnl_str = match t.pnl {
                    Some(p) => format!("${:.2} ({:.1}%)", p, t.pnl_pct.unwrap_or(0.0)),
                    None => "open".into(),
                };
                tracing::info!(
                    "  {} {} @ ${:.2} → {} | {} | {}",
                    t.side,
                    t.entry_number,
                    t.entry_price,
                    t.exit_price.map(|p| format!("${:.2}", p)).unwrap_or("--".into()),
                    pnl_str,
                    t.reason,
                );
            }
        }

        // Summary line for comparison
        if let Some(ref cache) = news_cache {
            tracing::info!("  NEWS CACHE: {} articles, {} news-active candles", cache.article_count(), news_signals);
        }
        tracing::info!("  SUMMARY: {} | Return={:.2}% | Sharpe={:.2} | DD={:.1}% | Win={:.1}% | Trades={} ({}L/{}S) | PF={:.2} | Signal {}L/{}S/{}N | NewsSigs={}",
            sym,
            result.total_return_pct,
            result.sharpe_ratio,
            result.max_drawdown_pct,
            result.win_rate,
            result.total_trades, long_trades, short_trades,
            result.profit_factor,
            long_count, short_count, neutral_count,
            news_signals,
        );
    }

    Ok(())
}

async fn cmd_live(config: &Config, store: &DataStore, dry_run: bool) -> anyhow::Result<()> {
    use atr_trader_v2::data::market::create_provider;
    use atr_trader_v2::dex::JupiterClient;
    use atr_trader_v2::live::{signal_config_from_config, LiveEngine};
    use atr_trader_v2::prod::ShutdownHandler;

    let mut live_config = config.live.clone();
    if dry_run {
        live_config.dry_run = true;
    }

    // Validate config before starting
    let data_dir = std::fs::canonicalize(&config.data_dir).unwrap_or_else(|_| config.data_dir.clone());
    let messages = atr_trader_v2::prod::validate_config(config, &data_dir);
    let has_errors = atr_trader_v2::prod::print_validation(&messages);
    if has_errors {
        anyhow::bail!("Config validation failed — fix errors and try again");
    }

    let signal_cfg = signal_config_from_config(config);
    let mut engine = LiveEngine::new_dry_run(live_config, signal_cfg)
        .with_store(store.clone())
        .with_market_provider(create_provider(&config.market_data.source, &config.market_data.timeframe))
        .with_jupiter(JupiterClient::default())
        .with_capital(config.live.initial_capital);

    // If wallet key is configured, load it
    if let Some(ref key) = config.dex.wallet_private_key {
        match atr_trader_v2::dex::Wallet::from_bs58(key) {
            Ok(wallet) => {
                engine = engine.with_wallet(wallet);
                tracing::info!("Wallet loaded");
            }
            Err(e) => {
                tracing::warn!("Failed to load wallet key: {e}. Running in dry-run mode.");
            }
        }
    }

    // Graceful shutdown handler
    let stop_signal = engine.stop_signal();
    let shutdown_handler = ShutdownHandler::new();

    // Spawn signal listener
    let handler_signal = shutdown_handler.signal();
    tokio::spawn(async move {
        shutdown_handler.run().await;
    });

    // Spawn a watcher that bridges the shutdown signal to the engine
    let engine_stop = stop_signal;
    let bridge_signal = handler_signal;
    tokio::spawn(async move {
        while !bridge_signal.load(std::sync::atomic::Ordering::Relaxed) {
            tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
        }
        engine_stop.store(true, std::sync::atomic::Ordering::Relaxed);
        tracing::info!("Shutdown signal received — engine stopping...");
    });

    tracing::info!(
        "Starting live trading engine (dry_run={}, symbols={})...",
        engine.dry_run,
        engine.status().symbols.join(", ")
    );
    engine.run().await?;

    tracing::info!("Live trading engine stopped. {} trades recorded.", engine.status().total_trades);
    Ok(())
}

async fn cmd_validate(config: &Config) -> anyhow::Result<()> {
    let data_dir = std::fs::canonicalize(&config.data_dir).unwrap_or_else(|_| config.data_dir.clone());
    let messages = atr_trader_v2::prod::validate_config(config, &data_dir);
    let has_errors = atr_trader_v2::prod::print_validation(&messages);

    if has_errors {
        tracing::error!("Validation FAILED — fix errors before running");
        std::process::exit(1);
    } else if messages.is_empty() {
        tracing::info!("✓ Config validation passed — no issues found");
    } else {
        tracing::info!("✓ Config valid (warnings above are informational)");
    }

    Ok(())
}

async fn cmd_check(config: &Config, store: &DataStore) -> anyhow::Result<()> {
    tracing::info!("=== Diagnostics ===");

    // 1. Config validation
    let data_dir = std::fs::canonicalize(&config.data_dir).unwrap_or_else(|_| config.data_dir.clone());
    let messages = atr_trader_v2::prod::validate_config(config, &data_dir);
    atr_trader_v2::prod::print_validation(&messages);

    // 2. Candle data coverage
    tracing::info!("── Candle Data:");
    for symbol in &config.market_data.symbols {
        let tf = &config.market_data.timeframe;
        match store.latest_candle_timestamp(symbol, tf).await {
            Ok(Some(ts)) => {
                let count = store.candle_count(symbol, tf).await.unwrap_or(0);
                let dt = chrono::DateTime::from_timestamp(ts, 0)
                    .map(|d| d.format("%Y-%m-%d %H:%M UTC").to_string())
                    .unwrap_or_else(|| ts.to_string());
                tracing::info!("  {symbol} ({tf}): {count} candles, latest {dt}");
            }
            Ok(None) => {
                tracing::warn!("  {symbol} ({tf}): no data. Run `fetch-candles`.");
            }
            Err(e) => {
                tracing::error!("  {symbol}: {e}");
            }
        }
    }

    // 3. News coverage
    tracing::info!("── News Data:");
    let latest_news = store.latest_news_timestamp().await.unwrap_or(None);
    match latest_news {
        Some(ts) => {
            let count = store.news_count().await.unwrap_or(0);
            let dt = chrono::DateTime::from_timestamp(ts, 0)
                .map(|d| d.format("%Y-%m-%d %H:%M UTC").to_string())
                .unwrap_or_else(|| ts.to_string());
            tracing::info!("  Articles: {count}, latest at {dt}");
        }
        None => {
            tracing::warn!("  No articles. Run `fetch-news` or `backfill-news`.");
        }
    }

    // 4. DEX connectivity test
    tracing::info!("── DEX:");
    tracing::info!("  Provider: {}", config.dex.dex);
    tracing::info!("  RPC: {}", config.dex.rpc_url);
    if config.dex.wallet_private_key.is_some() {
        tracing::info!("  Wallet: configured");
    } else {
        tracing::warn!("  Wallet: not configured — dry-run only");
    }

    // 5. Available signals
    tracing::info!("── Signals:");
    let signal_names: Vec<&str> = config.strategy.enabled_signals.iter().map(|s| s.as_str()).collect();
    if signal_names.is_empty() {
        tracing::warn!("  No signals enabled");
    } else {
        tracing::info!("  Enabled: {}", signal_names.join(", "));
    }

    tracing::info!("Check complete.");
    Ok(())
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // Load config
    let config = Config::load(&cli.config)?;

    // Setup logging
    let filter = EnvFilter::try_new(&config.log_level)
        .unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(true)
        .init();

    tracing::info!("ATR Trader V2 starting...");

    // Ensure data directory exists
    let data_dir = std::fs::canonicalize(&config.data_dir)
        .or_else(|_| {
            std::fs::create_dir_all(&config.data_dir)?;
            std::fs::canonicalize(&config.data_dir)
        })
        .map_err(|e| anyhow::anyhow!("Failed to resolve data dir {config_data_dir:?}: {e}", config_data_dir = &config.data_dir))?;

    // Validate command doesn't need DB
    if matches!(cli.command, Commands::Validate) {
        return cmd_validate(&config).await;
    }

    // Connect to database
    let db_path = data_dir.join("atr_trader.db");
    let db_str = db_path.to_str().ok_or_else(|| anyhow::anyhow!("Invalid db path"))?.to_string();
    tracing::info!("Connecting to database: {db_str}");
    let store = DataStore::connect(&db_str)
        .await
        .map_err(|e| anyhow::anyhow!("Failed to connect to database at {db_str}: {e}"))?;

    tracing::info!("Database ready");

    // Dispatch command
    match cli.command {
        Commands::FetchCandles { symbol, days, source } => {
            cmd_fetch_candles(&config, &store, symbol, days, source).await?
        }
        Commands::FetchNews { source } => {
            cmd_fetch_news(&config, &store, source).await?
        }
        Commands::FundingArb { live, dry_run, capital, positions, rebalance, cost_bps, threshold, days, exchange } => {
            cmd_funding_arb(&config, live, dry_run, capital, positions, rebalance, cost_bps, threshold, days, &exchange).await?
        }
        Commands::BackfillNews => {
            cmd_backfill_news(&config, &store).await?
        }
        Commands::Backtest { symbol, sweep, debug } => {
            cmd_backtest(&config, &store, symbol, sweep, debug).await?
        }
        Commands::Live { dry_run } => {
            cmd_live(&config, &store, dry_run).await?
        }
        Commands::Validate => {
            unreachable!("handled before DB connection")
        }
        Commands::Check => {
            cmd_check(&config, &store).await?
        }
    }

    Ok(())
}
