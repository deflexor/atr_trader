# NEXT.md — Live Trading Bot Build Plan

## Current Best Strategy

**EnhancedSignalGenerator** running on 7 assets concurrently.

**Proven result**: +8.94%/month across 8 assets (90-day backtest, 7/8 profitable).

### How to Run the Best Strategy

```bash
# Activate environment
source .venv/bin/activate

# Quick 30-day single-asset test (~15s)
python scripts/backtest/enhanced_signals_90d.py

# Full 90-day 8-asset backtest (~20 min)
python scripts/backtest/best_8assets_90d.py
```

### Best Config (copy-paste ready)

```python
from src.strategies.enhanced_signals import EnhancedSignalConfig, generate_enhanced_signal
from src.backtest.engine import BacktestConfig

SIGNAL_CONFIG = EnhancedSignalConfig(
    min_agreement=3,
    rsi_oversold=25.0,
    rsi_overbought=75.0,
    bollinger_required=True,
    breakout_lookback=100,
    breakout_min_range_pct=0.002,
    breakout_strength=0.8,
    mean_reversion_strength=0.7,
    trend_strength=0.8,
    vwap_enabled=False,
    divergence_enabled=False,
)

BACKTEST_CONFIG = BacktestConfig(
    initial_capital=1250.0,
    risk_per_trade=0.03,
    max_positions=2,
    cooldown_candles=96,
    use_trailing_stop=True,
    trailing_activation_atr=2.0,
    trailing_distance_atr=1.5,
    use_atr_stops=True,
    use_zero_drawdown_layer=False,
)

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "DOGEUSDT", "TRXUSDT",
    "SOLUSDT", "ADAUSDT", "AVAXUSDT",  # Drop UNIUSDT (only loser)
]
EXCHANGE = "bybit"
TIMEFRAME = "5m"
```

---

## Prompt: Build a Live Multi-Asset Crypto Trading Bot

Below is the complete prompt for building a production-grade live trading bot that
implements the proven EnhancedSignalGenerator strategy with robust safety features.

---

### SYSTEM PROMPT FOR LIVE BOT

You are building a production-grade live cryptocurrency trading bot called "PyPSiK Live".
The bot trades 7 crypto assets concurrently on Bybit exchange using 5-minute candles.

## CORE STRATEGY (PROVEN — DO NOT MODIFY)

The signal generation logic is already proven in backtesting at +8.94%/month.
Use the EXACT same pure functions from src/strategies/enhanced_signals.py:

- `compute_breakout_signal()`: N-candle high/low break (lookback=100)
- `compute_mean_reversion_signal()`: RSI < 25 + Bollinger touch
- `compute_trend_signal()`: EMA+RSI+MACD voting (min_agreement=3)
- `generate_enhanced_signal()`: Union logic with conflict cancellation and synergy bonus

Config:
  min_agreement=3, rsi_oversold=25.0, rsi_overbought=75.0,
  bollinger_required=True, breakout_lookback=100,
  breakout_min_range_pct=0.002, breakout_strength=0.8,
  mean_reversion_strength=0.7, trend_strength=0.8,
  vwap_enabled=False, divergence_enabled=False

Risk parameters:
  risk_per_trade=0.03 (3%), max_positions=2 per asset,
  cooldown=96 candles (8h), trailing_activation_atr=2.0,
  trailing_distance_atr=1.5, use_atr_stops=True

Assets: BTCUSDT, ETHUSDT, DOGEUSDT, TRXUSDT, SOLUSDT, ADAUSDT, AVAXUSDT
Exchange: Bybit, Timeframe: 5m

## ARCHITECTURE REQUIREMENTS

### 1. Event-Driven Core
- Async event loop (asyncio) with one task per symbol
- Each symbol has its own candle buffer (rolling window of 500+ candles)
- WebSocket connection to Bybit for real-time 5m candle updates
- On each new candle close: generate signal -> apply risk layer -> execute trade
- Never block the event loop — all I/O is async

### 2. Order Execution
- Use ccxt.async_support for Bybit API
- Market orders for entry (speed > price for 5m timeframe)
- Limit orders for exit (trailing stops submitted as conditional orders)
- Handle partial fills gracefully
- Retry logic: exponential backoff, max 3 retries
- Order confirmation: verify fill price within 0.5% of expected

### 3. Position Management
- Track positions in memory with periodic persistence to SQLite
- Trailing stop: compute ATR-based level each candle, update exchange stop if improved
- Cooldown enforcement: no new trades within 96 candles (8h) of last trade per symbol
- Max 2 concurrent positions per symbol
- Total portfolio exposure cap: max 60% of capital deployed

### 4. Risk Layer (LIVE-SAFE — CRITICAL)

Implement ALL of these safety mechanisms:

a) **Regime Detection** (src/risk/regime_detector.py — reuse)
   - Feed 1m candle returns to RegimeDetector.update()
   - Block ALL new trades when regime is CRASH
   - Reduce position size 50% in VOLATILE_TRENDING

b) **Pre-Trade Filter** (src/risk/pre_trade_filter.py — reuse)
   - Reject trades exceeding per-trade drawdown budget
   - Reject trades when total drawdown exceeds 5% daily budget

c) **Anti-Martingale** (built into engine — reuse)
   - Scale risk 1.25x on wins, 0.5x on 2+ consecutive losses
   - Cap at 1.5x base risk

d) **Daily Loss Limit** (NEW — for live)
   - If daily realized loss exceeds 3% of starting equity: HALT all trading for the day
   - Reset at 00:00 UTC
   - Send alert notification

e) **Max Open Drawdown** (NEW — for live)
   - If unrealized drawdown across ALL positions exceeds 8%: close all positions immediately
   - This is the nuclear circuit breaker

f) **Correlation Monitor** (src/risk/correlation_monitor.py — reuse)
   - Monitor BTC/ETH divergence as leading risk indicator
   - When correlation risk is HIGH: reduce new position sizes by 50%

g) **Rate Limiting** (NEW — for live)
   - Max 1 trade per symbol per 8 hours (96 candles)
   - Max 10 trades total per 24 hours across all symbols
   - Max 3 open positions total across portfolio at any time

### 5. Capital Management
- Starting capital: configurable, default $10,000
- Capital per asset: total_capital / 7 (equal weight)
- Position sizing: risk_per_trade * capital * max(signal.strength, 0.3) / price
- Track realized + unrealized P&L per asset
- Rebalance allocation weekly (or when an asset's drawdown exceeds 5%)

### 6. Data Pipeline
- Primary: Bybit WebSocket for real-time 5m candles
- Fallback: REST API polling every 30s if WebSocket disconnects
- Candle buffer: 500 candles per symbol (enough for all indicators)
- On startup: fetch last 500 candles via REST to warm up indicators
- Detect and handle candle gaps (missed candles, duplicate timestamps)
- Store all candles in SQLite for audit trail and strategy replay

### 7. Persistence & Recovery
- All state persisted to SQLite:
  - Current positions (entry price, size, stop level, trailing state)
  - Trade history (entry/exit timestamps, prices, PnL)
  - Equity curve (snapshot every candle)
  - Signal log (every signal generated, whether acted on or not)
- On crash restart:
  1. Load positions from DB
  2. Sync with exchange (reconcile any fills missed during downtime)
  3. Rebuild candle buffers from DB + recent REST fetch
  4. Resume trading with correct cooldown state

### 8. Monitoring & Alerting
- Telegram bot for real-time notifications:
  - Trade opened (symbol, direction, size, entry price, signal sources)
  - Trade closed (symbol, PnL, exit reason)
  - Risk alerts (regime change, daily loss limit hit, circuit breaker triggered)
  - Hourly status (equity, open positions, win rate, Sharpe)
- Web dashboard (simple FastAPI + static HTML):
  - Equity curve chart
  - Per-asset P&L table
  - Current positions
  - Signal history with source breakdown
- Health check endpoint for monitoring (uptime, last candle timestamp, connection status)

### 9. Error Handling
- Exchange API errors: retry with exponential backoff (1s, 2s, 4s)
- WebSocket disconnection: auto-reconnect within 5s, log gap
- Invalid candle data: skip candle, log warning
- Position sync failure: halt trading, alert operator
- Rate limit hit: back off 60s, then retry
- Insufficient balance: reduce position size to fit, log warning
- All errors logged with full context (symbol, timestamp, candle data, stack trace)

### 10. Configuration
- YAML config file (config/live.yaml):
  - Exchange credentials (API key, secret — from environment variables only!)
  - Trading parameters (all EnhancedSignalConfig + BacktestConfig values)
  - Risk limits (daily loss, max drawdown, rate limits)
  - Notification settings (Telegram bot token, chat ID)
  - Logging level and file path
- Environment variables for secrets (BYBIT_API_KEY, BYBIT_SECRET, TELEGRAM_TOKEN)
- Config validation on startup (all values within safe ranges)

### 11. Logging
- Structured JSON logging (timestamp, level, module, message, context)
- Separate log files: trading.log, risk.log, error.log
- Log rotation: 100MB per file, keep 10 files
- Every trade decision logged with:
  - All sub-signal values (breakout, mean-reversion, trend)
  - Indicator values (EMA, RSI, MACD, Bollinger, ATR)
  - Risk layer evaluation (regime, velocity, correlation, composite score)
  - Final decision (trade/no-trade with reason)

### 12. Testing Requirements
- Unit tests for all signal functions (already exist for pure functions)
- Integration test: replay 30 days of historical data through live engine
- Paper trading mode: same logic but orders are logged, not sent
- Dry run flag: run for N candles, print what would have been traded
- Strategy replay: take SQLite trade log, replay through backtest engine, verify same results

### 13. Deployment
- Docker container with health check
- Non-root user, read-only config
- Volume mount for data/ directory (SQLite persistence)
- Restart policy: unless-stopped
- Resource limits: 512MB RAM, 1 CPU core
- Graceful shutdown: close all positions on SIGTERM, wait 30s

### 14. Safety Checklist (MUST PASS BEFORE LIVE)

Before any real money deployment, verify ALL of these:

- [ ] Paper trading for minimum 7 days with consistent results matching backtest
- [ ] Daily loss limit triggers correctly in simulation
- [ ] Circuit breaker (8% unrealized DD) triggers correctly
- [ ] WebSocket reconnection works (test by killing connection)
- [ ] Crash recovery works (test by killing process, verify state restored)
- [ ] Position reconciliation with exchange works (test with manually placed order)
- [ ] All risk modules activated and logging correctly
- [ ] No API keys in code or config files (environment variables only)
- [ ] Telegram alerts working for all event types
- [ ] Cooldown enforcement verified (no rapid-fire trades)
- [ ] Starting with minimum viable capital (test with $100 first)

## PROJECT STRUCTURE

    src/live/
    ├── __init__.py
    ├── bot.py                  # Main event loop, orchestrator
    ├── candle_feed.py          # WebSocket + REST candle ingestion
    ├── executor.py             # Order execution with retry logic
    ├── position_manager.py     # Position tracking, trailing stops, persistence
    ├── risk_guard.py           # Live risk layer (daily loss, circuit breaker, rate limit)
    ├── capital_allocator.py    # Per-asset capital allocation + rebalancing
    ├── notifications.py       # Telegram bot for alerts
    ├── dashboard.py            # FastAPI web dashboard
    ├── config.py               # YAML config loader + validation
    ├── state.py                # SQLite persistence layer
    └── health.py               # Health check endpoints

## IMPLEMENTATION ORDER

1. **Phase A**: Candle feed (WebSocket + REST fallback) + signal generation (reuse pure functions)
2. **Phase B**: Paper trading executor (log decisions, no real orders)
3. **Phase C**: Position management (trailing stops, cooldown enforcement)
4. **Phase D**: Risk guard (daily loss limit, circuit breaker, regime detection)
5. **Phase E**: Persistence (SQLite state, crash recovery)
6. **Phase F**: Notifications (Telegram bot)
7. **Phase G**: Live execution (ccxt order placement with retry)
8. **Phase H**: Dashboard + monitoring
9. **Phase I**: Docker + deployment
10. **Phase J**: 7-day paper trading validation, then live with minimum capital

## CRITICAL REMINDERS

- The signal generation is PROVEN. Do NOT "improve" it. Copy the exact pure functions.
- The risk layer is the most important part. More bots blow up from missing risk checks than bad signals.
- Start with paper trading. No real money until paper trading matches backtest expectations.
- Never risk more than you can afford to lose completely.
- The 8h cooldown exists for a reason. Do not remove it for "more opportunities".
- UNIUSDT was the only losing asset in backtesting. Drop it for live.
- Test crash recovery BEFORE live deployment. This is non-negotiable.
