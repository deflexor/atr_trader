# Live Trading Module — Build Prompt

## Context

PyPSiK is a multi-asset crypto trading system with a proven backtested strategy. The backtest engine (`src/backtest/engine.py`) runs the `EnhancedSignalGenerator` across 8 assets on 5m candles and produces **+7.8%/month with -0.03% max drawdown** (trade-based, 22 consecutive profitable months). We now need a **live trading module** that executes real trades on Bybit spot.

## What Exists

### Working Code
- `src/strategies/enhanced_signals.py` — Pure function signal generator (breakout + mean-reversion + trend). Input: `CandleSeries`, output: `Signal`. This is the core alpha.
- `src/backtest/engine.py` — Backtest engine with position tracking, trailing stops, ATR stops, anti-martingale sizing, pyramiding, opposite-signal close. **The live module must replicate this exact logic.**
- `src/adapters/bybit_adapter.py` — Bybit WebSocket adapter for market data (tickers, orderbook). Has `fetch_orderbook()`, `fetch_ticker()`, `fetch_ohlcv_paginated()`. **No order execution methods yet.**
- `src/core/models/` — `Signal`, `Position`, `Order`, `Candle`, `CandleSeries`, `MarketData` models.
- `src/risk/` — Full risk layer: regime detector, composite risk scorer, drawdown budget, correlation monitor, adaptive sizer, velocity tracker.
- `src/core/db/datastore.py` — SQLite candle storage via `DataStore` class.
- `data/candles.db` — 6.5M candles (8 assets, 5m, multi-year).

### Fill Simulation (What We Know About Slippage)
`src/backtest/fills.py` uses volume-based slippage:
- Base slippage: 0.05% per trade
- Volume-adjusted: `slippage = factor * (1 + volume_ratio)`, capped at 1%
- 10% random noise added
- **Backtest avg commission per trade: $0.49** on ~$300-500 positions
- **Backtest avg PnL per trade: $3.46** (net of commission)
- **Slippage in live trading is the #1 risk** — if real fills are 2-3x worse than simulated, it would eat most of the profit. The live module MUST measure and adapt to real slippage.

## Requirements

### 1. Order Execution Engine (`src/execution/`)
- **Limit order priority**: Place limit orders at mid-price ± spread/2 instead of market orders to minimize slippage.
- **Smart order routing**: 
  - Calculate expected fill price from orderbook depth before placing
  - If spread is too wide (>$X), skip the trade
  - If orderbook is too thin, reduce position size or skip
- **Slippage guard**: Compare actual fill price vs signal price. If slippage exceeds threshold (e.g., 0.1%), log warning and adjust future sizing.
- **Order lifecycle**: PENDING → OPEN → PARTIAL/FILLED → CANCELLED. Handle partial fills gracefully.
- **Bybit authenticated API**: Add order placement/cancellation to `BybitAdapter` using ccxt or direct REST calls with HMAC signing.

### 2. Persistent State Database (`data/live_trading.db`)
All state must survive script restart / server reboot. Tables:

```sql
-- Open positions (restored on restart)
CREATE TABLE positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    side TEXT NOT NULL,  -- 'long' or 'short'
    status TEXT NOT NULL DEFAULT 'open',  -- 'open', 'closing', 'closed'
    entries TEXT NOT NULL,  -- JSON array: [{price, quantity, timestamp}]
    stop_loss REAL,
    take_profit REAL,
    trailing_stop REAL,
    trailing_activated INTEGER DEFAULT 0,
    highest_price REAL DEFAULT 0,
    lowest_price REAL DEFAULT 999999999,
    trailing_atr_multiplier REAL DEFAULT 2.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- All orders (audit trail)
CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    position_id TEXT,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    side TEXT NOT NULL,  -- 'buy' or 'sell'
    order_type TEXT NOT NULL,  -- 'market', 'limit'
    quantity REAL NOT NULL,
    price REAL,  -- limit price (NULL for market)
    status TEXT NOT NULL DEFAULT 'pending',
    filled_quantity REAL DEFAULT 0,
    avg_fill_price REAL,
    signal_price REAL,  -- price when signal was generated
    slippage_pct REAL,  -- actual slippage measured
    commission REAL DEFAULT 0,
    reason TEXT,  -- 'signal', 'trailing_stop', 'opposite_signal', 'stop_loss'
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Closed trades (for PnL tracking)
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,  -- 'long' or 'short'
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    quantity REAL NOT NULL,
    pnl REAL NOT NULL,
    commission REAL NOT NULL,
    slippage REAL NOT NULL,  -- total slippage cost
    exit_reason TEXT NOT NULL,
    duration_seconds INTEGER,
    created_at TEXT NOT NULL,
    closed_at TEXT NOT NULL
);

-- Equity snapshots (for PnL chart)
CREATE TABLE equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_equity REAL NOT NULL,  -- cash + unrealized PnL
    cash REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    daily_pnl REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0
);

-- Config/state
CREATE TABLE state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### 3. Resilient Trading Loop (`src/live/trader.py`)
The main trading loop must:

1. **On startup**:
   - Load all open positions from DB
   - Reconcile with exchange (check actual balances and open orders)
   - If positions exist on exchange but not in DB → adopt them
   - If positions in DB but not on exchange → mark as closed, record PnL
   - Resume candle processing from last known timestamp

2. **Each 5m candle**:
   - Fetch latest 5m candle from exchange
   - Run signal generator on last N candles (lookback window)
   - If signal fires → check risk layer → place order
   - Update trailing stops for all open positions
   - Check if any trailing/stop/TP triggered → close position
   - Record equity snapshot

3. **On shutdown** (SIGINT/SIGTERM):
   - Cancel all pending orders
   - Save state to DB
   - Keep positions open (they're on the exchange)
   - Clean exit

4. **Error handling**:
   - WebSocket disconnect → auto-reconnect (already in adapter)
   - API rate limit → exponential backoff
   - Order rejected → log and skip (don't retry blindly)
   - Partial fill → wait N seconds, then cancel remainder
   - Exchange downtime → pause trading, resume when recovered

### 4. Slippage Management

The backtest uses 0.05% base slippage. Live trading must be tighter:

- **Pre-trade check**: Fetch orderbook. Calculate expected fill price from top 3 levels. If expected slippage > 0.1% → skip trade.
- **Use limit orders**: Place limit at bid+1 tick (for buys) / ask-1 tick (for sells). Wait up to 30s for fill. If not filled → cancel and move on.
- **Measure actual slippage**: Store `signal_price` (mid-price when signal generated) and `avg_fill_price` in orders table. Compute `slippage_pct = (fill - signal) / signal * 100`.
- **Adaptive sizing**: Track rolling average slippage. If it exceeds 0.08%, reduce position size proportionally.
- **Blacklist thin books**: If an asset consistently has >0.15% spread, skip it (log warning).

### 5. PnL Tracking & Reporting

- **Real-time equity**: `equity = cash + sum(position.unrealized_pnl for open positions)`
- **Snapshot every 5 minutes** into equity table
- **Trade-level PnL**: Record entry/exit price, quantity, commission, slippage, duration
- **Daily summary**: Print daily PnL, win rate, total equity at end of each day
- **PnL chart data**: equity table supports building equity curves (same format as backtest)

### 6. Configuration

```python
@dataclass
class LiveTradingConfig:
    # Exchange
    exchange: str = "bybit"
    api_key: str = ""  # from env var BYBIT_API_KEY
    api_secret: str = ""  # from env var BYBIT_API_SECRET
    
    # Assets
    symbols: list[str] = field(default_factory=lambda: [
        "BTCUSDT", "ETHUSDT", "DOGEUSDT", "TRXUSDT",
        "SOLUSDT", "ADAUSDT", "AVAXUSDT", "UNIUSDT"
    ])
    
    # Sizing
    initial_capital: float = 10000.0
    risk_per_trade: float = 0.03  # 3%
    max_positions: int = 2  # per asset
    
    # Signal (same as backtest best config)
    signal_config: EnhancedSignalConfig = field(default_factory=EnhancedSignalConfig)
    
    # Timing
    timeframe: str = "5m"
    cooldown_candles: int = 96  # 8h
    
    # Slippage control
    max_slippage_pct: float = 0.10  # skip trade if expected > 0.1%
    max_spread_pct: float = 0.15  # skip if spread > 0.15%
    limit_order_wait_seconds: int = 30
    
    # Trailing stops (same as backtest)
    use_trailing_stop: bool = True
    trailing_activation_atr: float = 2.0
    trailing_distance_atr: float = 1.5
    use_atr_stops: bool = True
    
    # Risk layer
    use_zero_drawdown_layer: bool = True
    use_composite_risk: bool = True
```

## Architecture

```
src/
├── execution/
│   ├── __init__.py
│   ├── order_manager.py     # Order placement, tracking, limit-order logic
│   ├── slippage_guard.py    # Pre-trade slippage estimation + adaptive sizing
│   └── exchange_client.py   # Bybit authenticated API (ccxt wrapper)
├── live/
│   ├── __init__.py
│   ├── trader.py            # Main trading loop (async)
│   ├── state_manager.py     # DB persistence, position restore on restart
│   ├── candle_feed.py       # 5m candle provider (WS + REST fallback)
│   └── pnl_tracker.py       # Equity snapshots, trade logging, PnL reporting
└── (existing modules stay unchanged)
```

## Key Design Decisions

1. **Limit orders over market orders** — The backtest uses simulated fills at 0.05% slippage. Real market orders on Bybit spot for $200-500 positions on altcoins can easily see 0.2-0.5% slippage. Limit orders at mid-price capture the spread instead of paying it.

2. **SQLite for all state** — File-based, no external DB server needed. `aiosqlite` for async access. Same pattern as existing `DataStore`.

3. **Signal generator unchanged** — The `generate_enhanced_signal()` pure function takes `CandleSeries` and returns `Signal`. The live module builds the same `CandleSeries` from live candles and calls the same function.

4. **Position logic mirrors backtest engine** — Trailing stops, ATR calculation, pyramiding, opposite-signal close, anti-martingale sizing must behave identically to `BacktestEngine._process_signal()` and related methods.

5. **Graceful degradation** — If exchange API is down, the module pauses and waits. If WebSocket disconnects, reconnect with backoff. Never crash on transient errors.

## Files to Read First

- `src/backtest/engine.py` — Contains ALL the logic to replicate (position management, trailing stops, signal processing)
- `src/strategies/enhanced_signals.py` — Signal generator (pure function, reuse as-is)
- `src/adapters/bybit_adapter.py` — WebSocket adapter to extend with order execution
- `src/core/models/position.py` — Position model with trailing stop logic
- `src/core/models/order.py` — Order model with fill tracking
- `src/core/db/datastore.py` — SQLite pattern to follow
- `src/risk/composite_risk_scorer.py` — Risk layer integration
- `src/backtest/fills.py` — Current slippage model (understand the assumptions)

## Exit Criteria

- [ ] `src/execution/` module with order placement, limit order logic, slippage guard
- [ ] `src/live/trader.py` with main async trading loop
- [ ] `src/live/state_manager.py` with full DB persistence (all tables above)
- [ ] Position restore on restart from DB + exchange reconciliation
- [ ] PnL tracking with equity snapshots every 5 minutes
- [ ] Slippage measurement per trade (signal_price vs fill_price)
- [ ] Graceful shutdown on SIGINT/SIGTERM
- [ ] Config via environment variables (API keys) + dataclass
- [ ] `run_live.py` entry point script
