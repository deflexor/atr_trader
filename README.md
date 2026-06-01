# ATR Trader V2 — Funding Rate Arbitrage Bot

Delta-neutral crypto carry trade strategy. Collects funding payments every 8 hours
regardless of market direction. Backtested on 4 years of real Binance data across 18 assets.

## Performance

| Metric | Value |
|--------|-------|
| **Return** (4yr) | **+39.6%** |
| **Sharpe** | **1.16** |
| **Max DD** | **4.27%** |
| **CAGR** | ~10.5% |
| **Win rate** | 96%+ |
| **Costs** | 5 bps (maker rebate) |

> One year of real funding rates: +14.33% return, Sharpe 6.66, DD 0.16%.

## How It Works

1. Every 8h, funding rates settle on perpetual futures exchanges
2. Longs pay shorts when funding is positive (bull market) — we collect
3. We go **spot long + perp short** (delta-neutral), earning the rate
4. Top 3 symbols by rate, capped at 5 bps entry threshold, 14-day rebalance
5. Drawdown is tiny — the trade is direction-agnostic

## Quick Start

```bash
export BYBIT_API_KEY="***"
export BYBIT_API_SECRET="***"

# Dry run (no real orders, see what the bot would do):
cargo run --release -- funding-arb --live --dry-run

# Go live with $500:
cargo run --release -- funding-arb --live --capital 500
```

### Backtest

```bash
# 4 years of Binance data (18 symbols, ~2 min):
cargo run --release -- funding-arb --exchange binance

# Bybit snapshot + quick 66-day backtest:
cargo run --release -- funding-arb --exchange bybit
```

### CLI Options

```
--live        Live trading mode (requires BYBIT_API_KEY + BYBIT_API_SECRET)
--dry-run     Preview decisions without placing orders
--capital N   USD account size for position sizing (default: 500)
--positions N Maximum carry positions (default: 3)
--rebalance N Rebalance interval in days (default: 14)
--cost-bps X  Trading cost in bps — 5 for maker rebate (default: 5.0)
--threshold X Min funding rate per 8h (default: 0.00005 = ~5.5% APR)
--exchange    Data source: binance or bybit (default: bybit)
```

## Strategy Details

### Funding Rate Arb (primary, ~100% of capital)
- **Entry:** Pick top N symbols with funding rate ≥ threshold
- **Position:** Buy spot → Short same notional on perp — delta neutral
- **Exit:** Close when rate drops below threshold or 14 days elapsed
- **Edge:** Maker fee rebates (~5 bps) make it profitable; taker mode (10 bps) breaks even

### Exchange Support

| Exchange | Data History | Live Trading |
|----------|-------------|--------------|
| **Binance** | Full 4-yr history ✅ | Planned |
| **Bybit** | ~66 days (200 records) ✅ | **Live now** — spot + perp orders |

## Architecture

```
atr_trader_v2/
├── src/
│   ├── exchange/bybit.rs   — Bybit REST v5 client (auth, orders, tickers)
│   ├── live/
│   │   ├── engine.rs       — Legacy trade engine
│   │   └── funding_arb_engine.rs — Live funding arb event loop
│   ├── backtest/
│   │   ├── engine.rs       — Backtest engine
│   │   └── funding_arb_engine.rs — Funding arb backtester
│   ├── strategy/
│   │   └── funding_arb.rs  — Position logic (entry/exit/accrual)
│   ├── data/funding.rs     — Funding providers (Binance + Bybit)
│   ├── signals/            — Technical signal generators
│   ├── risk/               — Risk scoring and regime detection
│   ├── news/               — News sentiment scraper
│   └── config.rs           — Configuration
├── config.toml             — Bot config
└── Cargo.toml
```

## Requirements

- **Rust 1.75+**
- **Bybit API keys** (spot + perp trading enabled)
- **Maker fee tier** on Bybit (for 5 bps rebates)

Minimum deposit: **$200-500** (scales to 3 positions at ~$67-$167 each)
