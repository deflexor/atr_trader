# NEXT.md — Multi-Year Backtest & Results Pipeline

## Overview

Two scripts to build:

1. **`scripts/backtest/long_backtest.py`** — Multi-year historical backtest
2. **`scripts/backtest/generate_results.py`** — P/L charts + RESULTS.md

---

## Prompt: Build Multi-Year Backtest & Results Pipeline

---

### SCRIPT 1: Long Running Multi-Year Backtest

Build `scripts/backtest/long_backtest.py` that:

#### 1. Data Fetching (ByBit API)

- Fetch historical 5m candles from Bybit via ccxt for ALL available history
- Fetch from earliest available date (2019 for BTC, later for others) to latest
- Use incremental batch fetching: 1000 candles per request, resume from last timestamp
- Handle exchange rate limits with 100ms delay between requests
- Save each batch immediately to DB to avoid memory exhaustion
- Progress indicator: print every 10000 candles fetched

```
Symbols to fetch: BTCUSDT, ETHUSDT, DOGEUSDT, TRXUSDT, SOLUSDT, ADAUSDT, AVAXUSDT, UNIUSDT
Exchange: bybit
Timeframe: 5m
```

#### 2. Database Schema

Create new tables in `data/backtest_results.db` (SQLite):

```sql
CREATE TABLE backtest_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    completed_at TEXT,
    symbols TEXT,           -- JSON array
    days INTEGER,
    config_json TEXT,        -- JSON of strategy + backtest config
    total_return_pct REAL,
    monthly_return_pct REAL,
    max_drawdown_pct REAL,
    total_trades INTEGER,
    win_rate REAL,
    sharpe_ratio REAL,
    sortino_ratio REAL,
    equity_curve_json TEXT   -- JSON array of {timestamp, equity}
);

CREATE TABLE trade_log (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    symbol TEXT,
    side TEXT,
    entry_time TEXT,
    exit_time TEXT,
    entry_price REAL,
    exit_price REAL,
    quantity REAL,
    pnl REAL,
    commission REAL,
    exit_reason TEXT,
    signal_sources TEXT,     -- JSON array e.g. ["breakout", "trend"]
    signal_strength REAL,
    FOREIGN KEY (run_id) REFERENCES backtest_runs(id)
);

CREATE TABLE equity_snapshots (
    run_id INTEGER,
    timestamp TEXT,
    equity REAL,
    daily_return_pct REAL,
    unrealized_pnl REAL,
    open_positions INTEGER,
    FOREIGN KEY (run_id) REFERENCES backtest_runs(id)
);

CREATE TABLE signal_log (
    run_id INTEGER,
    timestamp TEXT,
    symbol TEXT,
    direction TEXT,
    strength REAL,
    sources JSON,
    regime TEXT,
    risk_score REAL,
    acted_on BOOLEAN,
    reason TEXT
);
```

#### 3. Backtest Engine (Multi-Year)

- For each symbol, load all historical candles from DB
- Run EnhancedSignalGenerator with best config (see below)
- Run each symbol sequentially (not in parallel — to avoid memory issues)
- Save EVERY trade to trade_log
- Save equity snapshots every 100 candles
- Save signal log every candle (for analysis)
- Compute performance metrics: total return, Sharpe, Sortino, max drawdown, win rate
- On completion: update backtest_runs table with final results

Best config to use:
```python
SIGNAL_CONFIG = EnhancedSignalConfig(
    min_agreement=3, rsi_oversold=25.0, rsi_overbought=75.0,
    bollinger_required=True, breakout_lookback=100,
    breakout_min_range_pct=0.002, breakout_strength=0.8,
    mean_reversion_strength=0.7, trend_strength=0.8,
    vwap_enabled=False, divergence_enabled=False,
)

BACKTEST_CONFIG = BacktestConfig(
    initial_capital=10000.0,
    risk_per_trade=0.03, max_positions=2,
    cooldown_candles=96, use_trailing_stop=True,
    trailing_activation_atr=2.0, trailing_distance_atr=1.5,
    use_atr_stops=True, use_zero_drawdown_layer=False,
)
```

#### 4. Progress & Resume

- Track progress in a `backtest_progress` table:
  - symbol, candles_fetched, candles_total, last_timestamp
- If script crashes, restart it and it should resume from where it left off
- Print estimated time remaining every 5 minutes
- Estimate based on candles processed per second

#### 5. Warning Message

At the start of the script, print a VERY visible warning:

```
================================================================================
  WARNING: This backtest will take several hours to complete.

  Fetching multi-year 5m candle data for 8 symbols...
  Estimated time:
    - Data fetching: 30-60 minutes (depending on API rate limits)
    - Backtest execution: 2-4 hours

  Results will be saved to data/backtest_results.db automatically.
  You can interrupt with Ctrl+C — progress is saved and resumable.

  To run in background:
    nohup python scripts/backtest/long_backtest.py > backtest.log 2>&1 &

  Check progress:
    tail -f backtest.log
================================================================================
```

#### 6. CLI Arguments

```python
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--symbols", nargs="+", default=ALL_SYMBOLS)
parser.add_argument("--days", type=int, default=None)  # None = all available
parser.add_argument("--run-id", type=int, help="Resume run ID")
parser.add_argument("--workers", type=int, default=1)  # 1 = sequential
args = parser.parse_args()
```

---

### SCRIPT 2: Results Generator

Build `scripts/backtest/generate_results.py` that:

#### 1. Read from DB

- Load all completed backtest runs from `backtest_runs` table
- Load trade_log for each run
- Load equity_snapshots for each run

#### 2. Generate P/L Charts

Use matplotlib to create charts:

```
results/charts/
├── equity_curve.png          # Equity over time with drawdown
├── monthly_returns.png       # Bar chart of monthly returns
├── asset_comparison.png      # Per-asset return comparison
├── drawdown_curve.png        # Drawdown over time
├── trade_pnl_distribution.png  # Histogram of trade P/L
├── win_rate_by_symbol.png    # Win rate per symbol
└── signal_source_pie.png     # Pie chart of signal sources
```

Charts should:
- Be clean and readable (white background, labeled axes, legends)
- Save as PNG at 150 DPI
- Include title, date range, strategy name

#### 3. Create RESULTS.md

Generate a comprehensive markdown report at `results/RESULTS.md`:

```markdown
# Backtest Results — Enhanced Signal Generator

**Date**: Generated on [date]
**Strategy**: EnhancedSignalGenerator (breakout + mean-reversion + trend)
**Period**: [start_date] to [end_date]
**Assets**: [list]

## Executive Summary

| Metric | Value |
|--------|-------|
| Total Return | +XX.XX% |
| Monthly Return (annualized) | +XX.XX% |
| Max Drawdown | XX.XX% |
| Sharpe Ratio | X.XX |
| Sortino Ratio | X.XX |
| Win Rate | XX.X% |
| Total Trades | XXXX |
| Profitable Assets | X/8 |

## Performance by Asset

| Symbol | Trades | Win Rate | Return | Max DD | Monthly |
|--------|--------|----------|--------|--------|---------|
| BTCUSDT | XXX | XX.X% | +X.XX% | X.XX% | X.XX% |
| ... | ... | ... | ... | ... | ... |

## Equity Curve

![Equity Curve](charts/equity_curve.png)

## Drawdown Analysis

- Max Drawdown: XX.XX%
- Average Drawdown: XX.XX%
- Time in Drawdown: XX%
- Longest Drawdown Period: X days

![Drawdown Curve](charts/drawdown_curve.png)

## Monthly Returns

| Month | Return | Equity |
|-------|--------|--------|
| 2024-01 | +X.XX% | $XXXX |
| ... | ... | ... |

![Monthly Returns](charts/monthly_returns.png)

## Signal Source Analysis

| Source | Count | % of Trades | Avg P&L |
|--------|-------|-------------|---------|
| breakout | XXX | XX.X% | $X.XX |
| trend | XXX | XX.X% | $X.XX |
| mean_reversion | XXX | XX.X% | $X.XX |

![Signal Sources](charts/signal_source_pie.png)

## Trade Statistics

- Average trade duration: X hours
- Average win: $X.XX
- Average loss: -$X.XX
- Risk/Reward ratio: X.XX
- Largest win: $X.XX
- Largest loss: -$X.XX

![P/L Distribution](charts/trade_pnl_distribution.png)

## Yearly Breakdown

| Year | Return | Monthly Avg | Trades | Win Rate |
|------|--------|------------|--------|----------|
| 2022 | +X.XX% | X.XX% | XXXX | XX.X% |
| 2023 | +X.XX% | X.XX% | XXXX | XX.X% |

## Improvement Guidelines

Based on the data analysis, the following improvements are recommended:

### 1. Signal Type Performance
- [Which signal type is most profitable]
- [Which signal type has worst win rate]

### 2. Asset Allocation
- [Best performing assets — consider increasing allocation]
- [Worst performing assets — consider reducing or dropping]

### 3. Entry Timing
- [Best days/times for entries based on trade success]
- [Times to avoid trading based on hourly/daily patterns]

### 4. Risk Management
- [Optimal stop loss distance based on ATR analysis]
- [Take profit level that maximizes Sharpe]

### 5. Cooldown Optimization
- [Optimal cooldown based on re-entry analysis]
- [Effects of longer/shorter cooldowns on returns]

### 6. Seasonality
- [Best months for this strategy]
- [Worst months — consider reduced sizing]
```

#### 4. Additional Analysis

- **Consecutive wins/losses** analysis — what is the max winning/losing streak
- **Time-of-day analysis** — which hours have best/worst returns
- **Day-of-week analysis** — which days are most profitable
- **Signal combination analysis** — which 2-signal combinations are most profitable
- **Recovery analysis** — how long does it take to recover from max drawdown

#### 5. CLI Arguments

```python
parser.add_argument("--run-id", type=int, default=None)  # Specific run or latest
parser.add_argument("--charts-only", action="store_true")  # Skip md, only charts
parser.add_argument("--md-only", action="store_true")  # Skip charts, only md
parser.add_argument("--output-dir", default="results")  # Results output directory
```

#### 6. Progress

- Print what is being generated: "Generating equity curve...", "Generating monthly returns..."
- Create output directory if it doesn't exist

---

## Implementation Notes

### Data Storage Strategy

- Store raw candles in `data/candles.db` (existing)
- Store backtest results in `data/backtest_results.db` (new)
- Store charts and RESULTS.md in `results/` (new directory)
- Use SQLite transactions for data integrity
- Compress equity_curve_json with zlib if > 1MB

### Performance Considerations

- Load candles from DB in chunks (not all at once)
- Use cursor iteration for large trade logs
- Generate charts with tight bounding boxes to keep file sizes small
- Parallelize chart generation (matplotlib is thread-safe for rendering)

### Chart Style

```python
import matplotlib.pyplot as plt
plt.style.use("seaborn-v0_8-whitegrid")
# Font size: 12 for labels, 14 for titles
# Figure size: 12x6 for most charts, 16x8 for equity curve
# Colors: blue for positive, red for negative
```

### Error Handling

- If a run_id doesn't exist, print error and list available runs
- If DB is empty, print "No backtest results found. Run long_backtest.py first"
- If chart generation fails, log warning but continue with other charts

---

## Quick Start

```bash
# Activate environment
source .venv/bin/activate

# Step 1: Run long backtest (takes hours!)
# WARNING: This will take several hours. Run in background.
nohup python scripts/backtest/long_backtest.py > backtest.log 2>&1 &

# Check progress
tail -f backtest.log

# Step 2: Generate results (after backtest completes)
python scripts/backtest/generate_results.py

# View results
open results/RESULTS.md
ls results/charts/
```
