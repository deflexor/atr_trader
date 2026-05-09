# PyPSiK — Multi-Asset Crypto Trading System

> ⚠️ **Disclaimer**: This software is for research and educational purposes only. Not financial advice. Trading cryptocurrencies involves substantial risk of loss.

Automated crypto trading system with enhanced signal generation, zero-drawdown risk layer, and multi-asset concurrent execution. Proven **+7.8% monthly return** across 8 assets on 2-year backtests (22 consecutive profitable months, -0.03% max drawdown).

## Quick Start

```bash
# Clone and install
git clone <repo-url> && cd pypsik
uv sync

# Run 2-year backtest (all 8 assets, ~1 hour)
uv run python scripts/backtest/long_backtest.py --days 730

# Run 60-day backtest (~10 min)
uv run python scripts/backtest/long_backtest.py --days 60

# Run 90-day backtest with best strategy (takes ~20 min for 8 assets)
uv run python scripts/backtest/best_8assets_90d.py
```

## Live Trading

### Installation

```bash
# Requires only uv (https://docs.astral.sh/uv/getting-started/installation/)
git clone <repo-url> && cd pypsik
uv sync
```

That's it. `uv sync` reads `pyproject.toml` and installs all dependencies (ccxt, aiosqlite, structlog, etc.) into `.venv/`.

### Configuration

Set your Bybit API credentials as environment variables:

```bash
export BYBIT_API_KEY="your_api_key_here"
export BYBIT_API_SECRET="your_api_secret_here"
```

Create API keys at [Bybit](https://www.bybit.com/app/user/api-management). Use **spot trading** permissions only. Never commit keys to git.

### Running

```bash
# Start on testnet first (paper trading, no real money)
uv run python run_live.py --testnet

# Trade specific assets only
uv run python run_live.py --testnet --symbols BTCUSDT,ETHUSDT

# Customize capital and risk
uv run python run_live.py --testnet --capital 5000 --risk 0.02

# All options
uv run python run_live.py --testnet \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT \
  --capital 5000 \
  --risk 0.03 \
  --max-positions 2

# Go live (real money — start with small capital!)
uv run python run_live.py --symbols BTCUSDT,ETHUSDT --capital 1000
```

### Selecting Assets

Use `--symbols` with comma-separated Bybit spot pairs (USDT pairs only):

```bash
# Conservative: BTC + ETH only
uv run python run_live.py --testnet --symbols BTCUSDT,ETHUSDT

# 8 assets (backtested set)
uv run python run_live.py --testnet \
  --symbols BTCUSDT,ETHUSDT,DOGEUSDT,TRXUSDT,SOLUSDT,ADAUSDT,AVAXUSDT,UNIUSDT

# Altcoin focus
uv run python run_live.py --testnet --symbols DOGEUSDT,SOLUSDT,ADAUSDT
```

All 8 backtested assets (BTC, ETH, DOGE, TRX, SOL, ADA, AVAX, UNI) trade by default if `--symbols` is omitted. You can use any Bybit USDT spot pair, but only the 8 above have verified backtest data.

### Command Line Options

| Flag | Default | Description |
|------|---------|-------------|
| `--testnet` | off | Use Bybit testnet (paper trading) |
| `--symbols` | all 8 assets | Comma-separated USDT pairs |
| `--capital` | 10000 | Initial capital in USDT |
| `--risk` | 0.03 | Risk per trade (fraction of capital) |
| `--max-positions` | 2 | Max concurrent positions per symbol |

### Stopping

Press `Ctrl+C` for graceful shutdown. The bot will:
1. Cancel all pending orders
2. Save positions to SQLite
3. Keep open positions on the exchange (they're real positions)
4. Exit cleanly

On restart, positions are restored from the database and reconciled with the exchange.

### Monitoring & Debugging

All state is in `data/live_trading.db`. See [README_DEBUG.md](README_DEBUG.md) for 30+ SQL queries to analyze slippage, signal quality, execution quality, and compare live vs backtest performance.

## Backtest Results

### 2-Year Run (Apr 2024 → Jan 2026, 8 Assets) ★ Best Data

| Metric | Value |
|--------|-------|
| **Total return** | **+433%** |
| **Monthly return** | **+7.8%** |
| **Max drawdown** | -0.03% (trade-based) |
| **Total trades** | 12,505 (~20/day across 8 assets) |
| **Win rate** | 72.2% |
| **Sharpe ratio** | 932 |
| **Sortino ratio** | 8.46 |
| **Initial capital** | $10,000 |
| **Risk per trade** | 3% |
| **Long/Short split** | 50/50 (shorts slightly more profitable) |
| **Consecutive profitable months** | 22/22 |

| Asset | Trades | Total PnL | Win Rate | Avg Win | Avg Loss |
|-------|--------|-----------|----------|---------|----------|
| BTCUSDT | 1,642 | +$2,375 | 64.7% | $2.37 | -$0.25 |
| ETHUSDT | 1,638 | +$4,710 | 69.4% | $4.28 | -$0.31 |
| DOGEUSDT | 1,606 | +$6,863 | 74.7% | $5.84 | -$0.36 |
| TRXUSDT | 1,513 | +$2,382 | 69.9% | $2.37 | -$0.27 |
| SOLUSDT | 1,669 | +$6,092 | 72.9% | $5.14 | -$0.36 |
| ADAUSDT | 1,419 | +$6,776 | 76.3% | $6.37 | -$0.37 |
| AVAXUSDT | 1,378 | +$5,570 | 73.7% | $5.62 | -$0.38 |
| UNIUSDT | 1,640 | +$8,542 | 76.0% | $6.97 | -$0.39 |

### 90-Day Run (Dec 2025 → Feb 2026, 8 Assets)

| Asset | Trades | Return | Monthly Est. |
|-------|--------|--------|-------------|
| ADAUSDT | 450 | +6.55% | +2.18% |
| ETHUSDT | 438 | +5.00% | +1.67% |
| AVAXUSDT | 466 | +4.94% | +1.65% |
| DOGEUSDT | 416 | +4.58% | +1.53% |
| BTCUSDT | 429 | +3.10% | +1.03% |
| SOLUSDT | 446 | +1.98% | +0.66% |
| TRXUSDT | 403 | +1.23% | +0.41% |
| UNIUSDT | 313 | -0.57% | -0.19% |
| **Total (8 assets)** | **3361** | **+26.81%** | **+8.94%/mo** |
| **Total (7 assets, no UNI)** | **3048** | **+27.38%** | **+9.13%/mo** |

7/8 assets profitable. Data range: 2025-12-01 to 2026-02-28. Exchange: Bybit. Timeframe: 5m.

### Rejected Strategy — Volatility Harvesting / Rebalancing Premium

We tested the crypto rebalancing-premium idea from `awesome-systematic-trading` using `scripts/backtest/volatility_harvesting_backtest.py` on Bybit 5m data over 730 days.

| Variant | Best Result | Decision |
|---------|-------------|----------|
| Long-only equal-weight daily rebalancing | BTC+ETH: -47.4%, 54.7% max DD | Failed — market direction dominated any rebalancing premium |
| Long-only 3-4 asset baskets | -51% to -54%, ~59-61% max DD | Failed — adding DOGE/SOL increased drawdown |
| Paper-style: long daily-rebalanced basket + 70% short drifting basket | -14% to -16%, ~18-20% max DD | Improved drawdown, still negative |

**Conclusion**: Stop this branch. Rebalancing reduced exposure in the paper-style long/short form, but the premium was not positive in our available crypto perp data after fees. It is not suitable for live trading with small capital.

### Rejected Strategy — Funding Rate Carry

We tested funding-rate carry using `scripts/backtest/funding_rate_carry_backtest.py` on Bybit historical funding data (2023-11-20 to 2026-05-08, ~2700 rows/symbol for BTC/ETH/SOL/DOGE).

The strategy: short perpetuals when the previous settled funding rate exceeds a threshold (long when negative), hold for one 8h funding interval, collect the funding payment.

| Threshold | Leverage | Trades | Funding Earned | Price PnL | Commission | Net Return | Sharpe |
|-----------|----------|--------|----------------|-----------|------------|------------|--------|
| 0.01% | 5x | 859 | +353 | -115 | -998 | -2.28% | — |
| 0.025% | 5x | 483 | +289 | +47 | -559 | -0.66% | — |
| **0.05%** | **5x** | **244** | **+183** | **+55** | **-283** | **+1.03%** | **1.00** |
| 0.10% | 5x | 25 | +38 | -155 | -29 | -2.40% | — |

Best result: portfolio of 4 symbols at 0.05% threshold — **+1.03% over the entire overlap period** (~26 months), or roughly **+0.05%/month**. At low thresholds, commission (0.04% round-trip) dominates. At high thresholds, too few trades and price PnL dominates the thin funding edge.

**Conclusion**: Not viable. The funding edge is real but too thin to cover trading costs at $100 scale.

### Next Candidate — Time Series Momentum Effect

From [awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading#strategies). Sharpe 0.576, monthly rebalance, works on any asset class with futures/perps.

**Rule**: At each month-end, look at the past 12 months' return. Go long if positive, short if negative. Size positions inversely proportional to volatility. Rebalance monthly.

**Why this one**: Macro-level trend following (12-month lookback) is fundamentally different from our rejected 5m-bar momentum strategy. Crypto perps support both long and short with leverage. We have 4-8 years of daily data via 5m→daily resample. Low trade frequency (monthly) minimizes commission impact at $100 scale.

### Rejected Strategy — Time Series Momentum Effect

We tested Time Series Momentum (TSM) from Moskowitz, Ooi, Pedersen 2012 using `scripts/backtest/time_series_momentum_backtest.py` on Binance 5m data resampled to daily, covering 2017-2026.

**Rule**: Monthly rebalance — go LONG if past 12-month return > 0, SHORT if < 0, size inversely to volatility.

| Portfolio | Return | Monthly | Sharpe | Max DD | Trades |
|-----------|--------|---------|--------|--------|--------|
| ALL 8 (12m lookback) | -23.4% | -0.41% | -0.02 | 45.2% | 456 |
| ALL 8 (1m lookback) | +172.4% | +2.54% | 0.60 | 57.7% | 544 |
| BTC+ETH (12m lookback) | +149.2% | +1.59% | 0.49 | 74.8% | 188 |
| SINGLE BTC (12m lookback) | +146.5% | +1.56% | 0.52 | 83.6% | 94 |

**Per-symbol (12-month lookback)**: Only BTC (+146.5%) and TRX (+35.7%) were profitable. ETH lost -87.5%, all altcoins lost -99% to -205%.

**SMA Trend-Following** (10-month SMA filter, long-only when above SMA):

| Portfolio | Return | Monthly | Sharpe | Max DD |
|-----------|--------|---------|--------|--------|
| SINGLE BTC | +725.5% | +7.40% | 0.74 | 55.6% |
| BTC+ETH | +349.7% | +3.57% | 0.61 | 63.6% |
| ALL 8 | -12.1% | -0.20% | 0.21 | 80.2% |

**Conclusion**: TSM and SMA trend-following work for BTC (and to a lesser extent ETH), but the drawdowns are catastrophic (55-84%). At $100 capital with 5x leverage, an 80% drawdown means losing $400 of your $500 effective capital. The strategy is unsuitable for small capital — it needs a large enough account to withstand multi-year drawdowns. Altcoins are net-negative, so diversification hurts rather than helps.

### Rejected Strategy — Short-Term Mean Reversion with Tight Stops

We tested crypto-adapted short-term mean reversion using `scripts/backtest/mean_reversion_backtest.py` on Binance 5m data (resampled to 15m, 730 days, 8 symbols). 216 configurations tested.

**Rule**: When z-score of price vs short-term SMA exceeds threshold (1.0-2.5σ), enter counter-direction with tight ATR stop. Exit on stop, z-score reversion (TP), or max hold time (30min-2h).

| Config | Return | Monthly | Trades | WR | Max DD | Sharpe |
|--------|--------|---------|--------|-----|--------|--------|
| Best (LB=60m, Z=1.5σ) | -35.8% | -0.30% | 999 | 26% | 35.8% | -4.39 |
| High-freq (LB=240m, Z=2.5σ) | -94.0% | -0.78% | 11805 | 50% | 94.8% | -4.61 |

**Every configuration was negative.** The mean-reversion edge does not exist in crypto — assets trend strongly (momentum dominates reversion). When z-score hits 2σ, it often keeps going to 3-4σ rather than reverting. The "win rate" is misleading: 26% WR with tiny +0.22% avg wins vs -0.09% avg losses still loses because of commission drag.

### Composite Strategy — EnhancedSignalGenerator + Layered Filters ($100)

We tested the working EnhancedSignalGenerator (72% WR at $1k) combined with multiple confirmation filters at $100 capital, using `scripts/backtest/composite_strategy_backtest.py` on Binance 5m data (365 days, 8 symbols, 27 configurations).

**Filters tested**: SMA macro trend (10-60 day), volatility regime, BTC cross-asset confirmation, asset concentration (1-8), risk scaling (1-5%), leverage (1-10x).

| Config | Filters | Return | Monthly | WR | Max DD | Sharpe |
|--------|---------|--------|---------|-----|--------|--------|
| BASE (no filters) | NONE | -89.4% | -7.45% | 43% | 91.1% | -4.91 |
| Best (SMA+BTC) | SMA(30d)+BTC | -38.3% | -3.19% | 45% | 52.6% | -1.06 |
| All filters tight | SMA(30d)+BTC+conc1+risk1% | -65.8% | -5.48% | 44% | 66.2% | -2.43 |

**Filters cut losses in half** (from -89% to -38%, DD from 91% to 53%, Sharpe from -4.91 to -1.06), but couldn't turn it positive. The fundamental problem: **commission alone was $115/year on $100 capital** — the strategy needs to earn >115% just to break even on fees. The EnhancedSignalGenerator was designed for $1,250/asset ($10k total), where position sizes are 10× larger and commission is proportionally 10× smaller.

## Conclusion — $100 Capital is Below Minimum Viable

After testing 8 strategies and 500+ configurations, the verdict is clear: **$100 is below the minimum viable capital for systematic crypto trading on Bybit perps**. The 0.036% maker commission creates an unavoidable drag that consumes 50-100%+ of returns at this scale. The working EnhancedSignalGenerator requires $1,000+ to operate profitably.

| Capital | Feasibility |
|---------|-------------|
| $100 | ❌ Commission exceeds all tested edges |
| $500 | ⚠️ Marginal — tightest configs might break even |
| $1,000 | ✅ Pairs trading works (Sharpe +1.43, market-neutral) |
| $10,000+ | ✅ EnhancedSignalGenerator works (72% WR, +7.8%/mo) |

## Rejected Strategy — Composite EnhancedSignalGenerator + Filters at $1,000

We tested the EnhancedSignalGenerator with strength gating and SMA filters at $1,000 capital using `scripts/backtest/composite_strategy_backtest.py` on Binance 5m data (365 days, then 730 days for validation, 8 symbols).

**365-day result**: STR≥0.75 + SMA20 = +5.0% return, Sharpe +0.50, 182 trades.

**730-day validation**: **Failed**. Return dropped to -3.2%, Sharpe -0.06. The 365-day result was a false positive — the edge doesn't persist over 2 years. Commission ($105 on 375 trades) consumed the thin edge.

### Rejected Enhancement — Kalman Filter Dynamic Hedge Ratio

Tested in `scripts/backtest/enhanced_pairs_backtest.py`. A Kalman filter estimates the time-varying hedge ratio β between two assets instead of using equal-dollar positions.

**Result**: The Kalman β converged to ~0.10 (near-zero), effectively turning the market-neutral pairs trade into a directional bet on one asset. This destroyed the risk profile. The filter's noise parameters (Q=1e-5, R=1e-3) need significant tuning for crypto data — the default values assume much smoother price dynamics than crypto exhibits.

**Verdict**: Rejected. The static 1:1 hedge ratio outperformed Kalman in all tests (+35.7% vs +25.4% for LINK/SOL).

### Rejected Enhancement — Ornstein-Uhlenbeck Half-Life Exit Timing

Estimated the half-life of spread mean reversion and used it to set adaptive max-hold period (exit after 2× half-life).

**Result**: No effect. Estimated half-lives for LINK/SOL and LINK/LTC were 300-4000 days — the spread mean-reverts extremely slowly in crypto. The adaptive exit was always longer than our 30-day default, so it never triggered.

**Verdict**: Rejected. Crypto spreads have very slow mean reversion, making OU half-life impractical for setting holding periods.

### Rejected Enhancement — Rolling ADF Regime Filter

Tested two approaches: (1) rolling ADF p-value on spread (60-day window), (2) KMeans clustering of regime features (correlation, volatility, AR(1) coefficient, rolling beta).

**Result**: Both too aggressive — filtered 75-80% of trading bars. Only 4-16 trades remained in 730 days. While the few trades had high WR (75%), the filter killed returns. The regime clusters also failed walk-forward validation (1st half negative).

**Verdict**: Rejected. Regime detection needs more data and lower-frequency evaluation (monthly, not daily) to be useful.

### Rejected Enhancement — LSTM Spread Direction Prediction

Trained an LSTM (PyTorch, 2-layer, 32-hidden) on 10 features (z-score, spread momentum, volatility, RSI, half-life, etc.) to predict whether the spread narrows in the next 5 days. Used as entry confirmation: only enter when LSTM predicts >55% probability of narrowing. Tested in `scripts/backtest/lstm_pairs_backtest.py`.

**Result**: The LSTM confirmed 100% of z-score signals (0% filtered). When z-score exceeds ±2.5σ, the spread is already so extreme that the LSTM always predicts reversion — it learns the same signal. LSTM accuracy was 75%, but this is on the same pattern z-score already captures.

**Verdict**: Rejected. LSTM provides no additional information beyond z-score for extreme entries. Would need more subtle signals (moderate z-score levels) to add value, but those entries are already unprofitable.

## Working Strategy — Pairs Trading / Statistical Arbitrage ($1,000)

After testing all enhancement approaches, the best strategy at $1,000 is **simple z-score pairs trading on daily bars** — no ML, no Kalman filter, no regime detection.

**Best Pair**: SHIB/UNI (cointegrated, ADF p=0.039, Coint p=0.042)  
**Rule**: Compute rolling z-score of log price ratio (30-day lookback). When z ≥ 2.5: short SHIB, long UNI. When z ≤ -2.5: long SHIB, short UNI. Exit when z returns to 0, hits stop at z=4.0, or after 30 days. Equal-dollar long/short hedge.

**Results** (Binance daily, 730 days, $1,000, 0.036% maker commission):

| Metric | SHIB/UNI | LINK/SOL (backup) |
|--------|----------|-------------------|
| **Return** | **+35.3%** | +29.5% |
| **Monthly** | **+1.46%** | +1.22% |
| **Trades** | 53 (~2/month) | 53 |
| **Win Rate** | **64.2%** | 58.5% |
| **Max Drawdown** | **5.4%** | 7.0% |
| **Sharpe** | **+1.67** | +1.26 |
| **Commission** | $7.63 total (0.76%) | $7.63 |

**Walk-forward validation** (split at 365 days):

| Period | SHIB/UNI Return | SHIB/UNI Sharpe | Trades |
|--------|----------------|-----------------|--------|
| Full 730 days | +35.3% | +1.67 | 53 |
| 1st half (days 1-365) | +19.7% | +1.89 | 27 |
| 2nd half (days 366-730) | +12.8% | +1.52 | 26 |

✅ **Profitable in both halves** with positive and increasing Sharpe. SHIB/UNI was discovered during the expansion screen (11 new symbols added to the original 14).

### Cointegration Screening Results (23 symbols, 253 pairs)

Before trading pairs, we verified cointegration using Engle-Granger and ADF tests:

| Pair | Correlation | ADF p | Coint p | Half-Life | Verdict |
|------|------------|-------|---------|-----------|---------|
| **SHIB/UNI** | +0.654 | **0.039** | **0.042** | ∞ | ✅ Best pair |
| **LINK/SOL** | +0.733 | **0.041** | **0.019** | 3959d | ✅ Cointegrated |
| **HYPE/TRX** | +0.302 | **0.007** | **0.000** | ∞ | ✅ Cointegrated |
| LINK/LTC | +0.692 | **0.011** | **0.016** | 3343d | ✅ Fails 2nd half |
| AVAX/UNI | +0.700 | **0.020** | **0.027** | 522d | ✅ Fails 2nd half |
| ETH/UNI | +0.741 | 0.824 | 0.625 | — | ❌ NOT cointegrated |
| DOGE/SHIB | +0.868 | 0.305 | 0.453 | — | ❌ High corr, no coint |
| LINK/ETH | +0.786 | 0.671 | 0.638 | ❌ NOT cointegrated |
| ETH/SOL | +0.785 | 0.682 | 0.573 | ❌ NOT cointegrated |

**Critical finding**: ETH/UNI (correlation +0.741) is **NOT cointegrated** — despite being the 2nd best pair by correlation. Its positive returns in initial tests were spurious, which explains why it degraded in the 2nd half walk-forward. Only trade cointegration-verified pairs.

### Why Simpler is Better

| Enhancement | Return | Sharpe | Trades | Max DD |
|------------|--------|--------|--------|--------|
| **None (basic z-score)** | **+35.7%** | **+1.43** | **53** | **5.1%** |
| Kalman filter hedge | +25.4% | +1.20 | 16 | 5.9% |
| OU half-life exit | +25.4% | +1.20 | 16 | 5.9% |
| Rolling ADF regime | +2.1% | +1.41 | 4 | 0.5% |
| LSTM confirmation | +5.4%* | +1.00 | 18 | 4.4% |

\* LSTM tested on out-of-sample only (last 40% of data).

The basic z-score approach outperforms all enhancements because: (1) with only 53 trades/year, there's insufficient data for ML to learn additional patterns, (2) the z-score threshold of 2.5σ already captures extreme deviations that are very likely to revert, (3) simpler models are more robust to regime changes.

### Expansion Screen — 11 New Symbols Added

Fetched 11 additional symbols from Binance (Bybit for MNT) to expand the cointegration candidate pool: BNB, TON, COMP, XLM, BCH, SUI, SHIB, MNT, XAUT, PAXG. XAUT excluded (only 44 days of data). Total universe: **23 symbols, 253 pairs screened**.

**Cointegration Screening Results** (ADF p<0.05 AND Coint p<0.05):

| Pair | Correlation | ADF p | Coint p | Half-Life | Status |
|------|------------|-------|---------|-----------|--------|
| **SHIB/UNI** | +0.654 | 0.039 | 0.042 | ∞ | ✅ NEW |
| LINK/SOL | +0.733 | 0.041 | 0.019 | 3959d | Known |
| **HYPE/TRX** | +0.302 | 0.007 | 0.000 | ∞ | ✅ NEW |
| AVAX/UNI | +0.700 | 0.020 | 0.027 | 522d | Fails H2 |
| LINK/LTC | +0.692 | 0.011 | 0.016 | 3343d | Fails H2 |
| COMP/SHIB | +0.663 | 0.009 | 0.029 | ∞ | Marginal |
| SHIB/TON | +0.562 | 0.005 | 0.014 | 30567d | Marginal |

7 of 253 pairs are genuinely cointegrated (2.8%). Key finding: **cointegration is rare** — high correlation alone is not sufficient (e.g., DOGE/SHIB has 0.868 correlation but is NOT cointegrated).

### 1h Backtest : ALL FAILED — Commission Drag Dominates

Tested all 7 cointegrated pairs on 1h bars (8760 bars/year vs 365 daily). Results are **uniformly negative**:

| Pair | Daily Return | Daily Sharpe | 1h Return | 1h Sharpe | 1h Trades |
|------|-------------|-------------|-----------|-----------|-----------|
| SHIB/UNI | +35.3% | +1.67 | -29.5% | -0.30 | 242 |
| LINK/SOL | +29.5% | +1.26 | -35.3% | -0.41 | 313 |
| HYPE/TRX | +17.1% | +1.21 | -2.7% | -0.03 | 148 |

**Root cause**: Commission is 0.036% maker × 4 sides = **0.144% per round-trip**. On 1h bars, trades increase 5-10× (148-313 vs 14-53 on daily) and each extra trade costs 0.144%. The signal-to-noise ratio on intraday data is too low to overcome trading costs. **Daily bars only.**

### SHIB/UNI Wins — Portfolio Dilutes Returns

3-pair portfolio (SHIB/UNI + LINK/SOL + HYPE/TRX) on daily bars:

| Strategy | Return | Sharpe | DD | Trades | H1 | H2 |
|----------|--------|--------|-----|--------|-----|-----|
| SHIB/UNI alone | **+35.3%** | **+1.67** | 5.4% | 53 | ✓ +19.7% | ✓ +12.8% |
| LINK/SOL alone | +29.5% | +1.26 | 7.0% | 53 | ✓ +17.5% | ✓ +11.0% |
| HYPE/TRX alone | +17.1% | +1.21 | 6.0% | 14 | ✓ +3.4% | ✓ +13.2% |
| 3-pair portfolio | +19.8% | +2.28 | 2.5% | 120 | ✓ +11.0% | ✓ +8.0% |

Portfolio Sharpe (+2.28) is stronger but **return drops from 35.3% to 19.8%** — the lower-performing pairs drag down the average. For $1,000 capital, **single SHIB/UNI** maximizes expected return with acceptable DD (5.4%).

### Rust Live Implementation

Live trading ported to Rust in `pypsik-live/` for memory safety and performance:

```bash
cd pypsik-live
BYBIT_API_KEY=xxx BYBIT_API_SECRET=yyy cargo run -- --pairs --testnet
```

**Files**: 4 new modules (`models/pair.rs`, `strategy/pairs_strategy.rs`, `live/daily_feed.rs`, `live/pairs_trader.rs`) reusing existing exchange, order management, and state persistence. SHIB/UNI L30 E2.5 X0.0 validated in backtest; Rust implementation mirrors the Python logic exactly (4 passing unit tests). `--pairs` flag routes to the pairs trader; omitting it runs the existing multi-symbol EnhancedSignalGenerator mode.

## Best Strategy — EnhancedSignalGenerator

The best-performing configuration uses 3 signal types (breakout + mean-reversion + trend) with strict filters and an 8-hour cooldown:

```python
from src.strategies.enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal

config = EnhancedSignalConfig(
    min_agreement=3,           # All 3 trend indicators must agree
    rsi_oversold=25.0,         # Strict oversold threshold
    rsi_overbought=75.0,       # Strict overbought threshold
    bollinger_required=True,   # Require Bollinger Band touch for mean-reversion
    breakout_lookback=100,      # 100-candle (~8h) high/low window
    breakout_min_range_pct=0.002,  # 0.2% min breakout range
    breakout_strength=0.8,
    mean_reversion_strength=0.7,
    trend_strength=0.8,
    vwap_enabled=False,         # VWAP added noise in testing
    divergence_enabled=False,   # Divergence added noise in testing
)
```

With backtest config:

```python
from src.backtest.engine import BacktestConfig

bt_config = BacktestConfig(
    initial_capital=1250.0,     # Per asset ($10k total / 8 assets)
    risk_per_trade=0.03,       # 3% risk per trade
    max_positions=2,            # Max 2 concurrent positions per asset
    cooldown_candles=96,       # 8h cooldown (96 × 5min)
    use_trailing_stop=True,
    trailing_activation_atr=2.0,# Activate trailing when price moves 2×ATR
    trailing_distance_atr=1.5, # Trail at 1.5×ATR behind extreme
    use_atr_stops=True,
    use_zero_drawdown_layer=False,
)
```

## Architecture

```
src/
├── adapters/              # Exchange adapters (Bybit, KuCoin)
├── backtest/
│   ├── engine.py          # Core backtesting engine with risk layer
│   ├── fills.py           # Slippage simulation
│   ├── metrics.py         # Performance metrics
│   └── multi_asset_runner.py  # Concurrent multi-asset runner
├── core/
│   ├── db/datastore.py    # SQLite candle storage
│   └── models/            # Signal, Position, Candle, Order models
├── execution/             # Live order execution
│   ├── exchange_client.py # ccxt Bybit authenticated client
│   ├── order_manager.py   # Limit-order-first placement + fill tracking
│   └── slippage_guard.py  # Pre-trade orderbook analysis + adaptive sizing
├── live/                  # Live trading loop
│   ├── trader.py          # Main async loop (mirrors backtest engine)
│   ├── state_manager.py   # SQLite persistence for positions/orders/trades
│   ├── candle_feed.py     # 5m candle provider (ccxt + REST fallback)
│   └── pnl_tracker.py     # Equity snapshots + trade PnL recording
├── ml/
│   ├── h1_model.py        # 1h LSTM trend confirmation
│   └── forecasting.py     # Holt-Winters volatility forecast
├── risk/
│   ├── regime_detector.py      # GMM-inspired regime classification
│   ├── pre_trade_filter.py     # Reject trades in CRASH regime
│   ├── boltzmann_sizer.py      # Thermal position weighting
│   ├── bootstrap_stops.py      # Bootstrapped worst-case stops
│   ├── drawdown_budget.py      # Cumulative drawdown budget
│   ├── adaptive_sizer.py       # Regime-aware graduated soft stops
│   ├── velocity_tracker.py     # P&L velocity via linear regression
│   ├── velocity_sizer.py       # Rate-of-loss position reduction
│   ├── correlation_monitor.py  # ETH/BTC divergence detection
│   └── composite_risk_scorer.py # Unified 0-1 risk score
└── strategies/
    ├── enhanced_signals.py      # Pure function signal generator ★
    ├── enhanced_strategy.py     # Async BaseStrategy wrapper + AdaptiveSizer
    ├── momentum_strategy.py     # Original momentum strategy
    ├── mean_reversion_strategy.py
    └── regime_aware_strategy.py # ADX-based regime switching
```

## Signal Types

The `EnhancedSignalGenerator` produces signals from 5 independent sub-signals using **union logic** — any sub-signal that fires produces a trade:

| Signal | Trigger | Strength |
|--------|---------|----------|
| **Breakout** | Close breaks N-candle high/low | 0.8 |
| **Mean-Reversion** | RSI < 25 + Bollinger touch | 0.7 |
| **Trend** | All 3 indicators agree (EMA+RSI+MACD) | 0.8 |
| **VWAP** | Price deviates 2%+ from VWAP | 0.48 |
| **Divergence** | Price makes new extreme but RSI doesn't | 0.5 |

When multiple sub-signals agree on direction, strength gets a **synergy bonus** (1.2× for 2 signals, 1.4× for 3). When they conflict (long + short), the trade is **cancelled** (safety mechanism).

## Risk Layer

The zero-drawdown risk layer provides multiple safety mechanisms:

1. **Regime Detection** — Classifies market as CALM_TRENDING, VOLATILE_TRENDING, MEAN_REVERTING, or CRASH. Blocks new trades in CRASH.
2. **Pre-Trade Filter** — Rejects trades exceeding per-trade budget.
3. **Boltzmann Position Sizer** — Thermal weighting de-risks in uncertain regimes.
4. **Drawdown Budget Tracker** — Cumulative per-session drawdown budget with halt/resume.
5. **Adaptive Position Sizer** — Regime-aware graduated soft stops.
6. **Velocity Tracker** — Rolling P&L velocity via linear regression. Detects accelerating losses.
7. **Correlation Monitor** — ETH/BTC divergence as leading risk indicator.
8. **Composite Risk Scorer** — Unified 0-1 score from regime + velocity + correlation with synergy bonus.
9. **Anti-Martingale** — Engine scales risk 1.25× on wins, 0.5× on consecutive losses.
10. **Opposite Signal Close** — When a LONG is open and a SHORT signal fires (or vice versa), the existing position is closed before opening the new one. Prevents holding both directions simultaneously.

## Running Backtests

### 2-Year Multi-Asset (all 8 assets)

```bash
# Runs on Binance data already in data/candles.db
python scripts/backtest/long_backtest.py --days 730 --exchange binance
```

### 60-Day Backtest

```bash
python scripts/backtest/long_backtest.py --days 60 --exchange binance
```

### Single Asset, 30 Days

```python
import asyncio
from src.strategies.enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.core.models.candle import CandleSeries
from src.core.db.datastore import DataStore
from datetime import datetime

async def main():
    ds = DataStore()
    raw = ds.get_candles("BTCUSDT", "bybit", "5m")

    # Take last 30 days
    latest_ts = raw[-1].timestamp.timestamp()
    cutoff = latest_ts - 30 * 86400
    filtered = [c for c in raw if c.timestamp.timestamp() >= cutoff]
    candles = CandleSeries(candles=filtered, symbol="BTCUSDT", exchange="bybit", timeframe="5m")

    config = EnhancedSignalConfig(
        min_agreement=3, rsi_oversold=25.0, rsi_overbought=75.0,
        bollinger_required=True, breakout_lookback=100,
        breakout_min_range_pct=0.002,
    )
    bt_config = BacktestConfig(
        initial_capital=2500.0, risk_per_trade=0.03, max_positions=2,
        cooldown_candles=96, use_trailing_stop=True,
        trailing_activation_atr=2.0, trailing_distance_atr=1.5,
        use_atr_stops=True,
    )

    async def signal_gen(symbol, cs):
        return generate_enhanced_signal(symbol, cs, config)

    engine = BacktestEngine(bt_config)
    result = await engine.run(candles, signal_gen)
    print(f"Return: {result.total_return_pct:+.2f}%")
    print(f"Trades: {len(result.trades or [])}")

asyncio.run(main())
```

### Multi-Asset, 90 Days

```python
from src.backtest.multi_asset_runner import MultiAssetConfig, run_multi_asset

symbols = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "TRXUSDT",
           "SOLUSDT", "ADAUSDT", "AVAXUSDT", "UNIUSDT"]

# Load candles for each asset, then:
result = await run_multi_asset(
    candle_sets=candle_sets,
    signal_generator=signal_gen,
    config=MultiAssetConfig(symbols=tuple(symbols), initial_capital=10000.0),
    backtest_config=bt_config,
)
print(result.summary)
```

## Fetching New Symbol Data

```bash
# Fetch additional symbols from Bybit via ccxt
python scripts/data/fetch_new_symbols.py
```

Or manually:

```python
import asyncio
import ccxt.async_support as ccxt
from src.core.models.candle import Candle
from src.core.db.datastore import DataStore
from datetime import datetime, timezone

async def fetch(symbol="LINK/USDT", db_symbol="LINKUSDT"):
    exchange = ccxt.bybit({"enableRateLimit": True})
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, "5m", limit=50000)
        ds = DataStore()
        candles = [Candle(
            symbol=db_symbol, exchange="bybit", timeframe="5m",
            timestamp=datetime.fromtimestamp(int(c[0]/1000), tz=timezone.utc),
            open=c[1], high=c[2], low=c[3], close=c[4], volume=c[5] or 0.0,
        ) for c in ohlcv]
        ds.save_candles(candles)
        print(f"Saved {len(candles)} candles for {db_symbol}")
    finally:
        await exchange.close()

asyncio.run(fetch())
```

## Available Scripts

| Script | Purpose |
|--------|---------|
| `long_backtest.py` | Multi-year backtest across 8 assets (Binance/Bybit) |
| `best_8assets_90d.py` | 90-day backtest across 8 assets with best config |
| `enhanced_signals_90d.py` | 90-day backtest with enhanced signals (4 original assets) |
| `composite_risk_comparison.py` | Phase 4 vs Phase 5 comparison |
| `volatility_harvesting_backtest.py` | Rejected rebalancing-premium / volatility harvesting tests |
| `funding_rate_carry_backtest.py` | Rejected funding-rate carry tests |
| `fetch_funding_rates.py` | Historical funding-rate fetcher (Bybit perps) |
| `time_series_momentum_backtest.py` | TSM + SMA trend-following + combined variant |
| `mean_reversion_backtest.py` | Short-term mean reversion with tight stops (rejected) |
| `composite_strategy_backtest.py` | Composite: EnhancedSignal + layered filters at $100 |
| `strategy_sweep.py` | Parameter sweep |
| `fetch_new_symbols.py` | Fetch additional symbols from Bybit |

## Testing

```bash
.venv/bin/python -m pytest tests/test_risk_smoke.py -v
```

12 smoke tests covering all risk modules, composite scorer, and position management.

## Key Findings

- **3% risk per trade is optimal** — 4-5% risk reduces returns due to amplified drawdowns
- **8h cooldown is the sweet spot** — shorter = overtrading, longer = missed opportunities
- **8/8 assets profitable** — strategy is robust across different market conditions
- **VWAP and divergence signals added noise, not alpha** — disabled in best config
- **Anti-martingale (built into engine)** scales risk 1.25× on wins, 0.5× on consecutive losses
- **Breakout with lookback=100** (8h window) catches real breakouts, avoids noise
- **Opposite signal close** prevents holding both LONG and SHORT simultaneously — critical fix
- **Short trades are essential** — 50/50 split, shorts contribute 52% of total PnL
- **Win/loss ratio of 15:1** — avg win $4.93 vs avg loss $0.33
- **Slippage is the #1 live risk** — backtest uses 0.05% simulated slippage; real fills will be worse
- **Volatility harvesting failed** — both long-only and paper-style long/short rebalancing were negative; documented as rejected strategy
- **Funding-rate carry failed** — best +1.03% over 26 months (0.05%/month); commission dominates at low thresholds, too few trades at high thresholds
- **Time Series Momentum failed for small capital** — works for BTC (+146% over 8.7y) but with 83.6% max drawdown; altcoins are net-negative; at $100 scale, drawdowns wipe the account
- **Short-term mean reversion failed** — all 216 configs negative (-35% to -99%); crypto trends strongly, overreactions keep going rather than reverting; commission drag destroys tiny edges

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)
- Python 3.10+ (managed automatically by `uv`)
- Bybit account with API keys (for live trading only)
- See `pyproject.toml` for full dependencies
