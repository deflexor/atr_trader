# Debugging Live Trading

All trading state is stored in `data/live_trading.db` (SQLite). Every order and trade has a `context` JSON column with the full market picture at the time of execution.

## Quick Start

```bash
sqlite3 data/live_trading.db

-- Today's trades with PnL
SELECT symbol, side, exit_reason, pnl, commission, slippage,
       duration_seconds/60 as minutes,
       context
FROM trades
WHERE date(closed_at) = date('now')
ORDER BY closed_at DESC;

-- Equity curve
SELECT timestamp, total_equity, cash, unrealized_pnl, open_positions, daily_pnl
FROM equity
ORDER BY id DESC LIMIT 50;
```

## Tables

| Table | What it stores |
|-------|---------------|
| `positions` | Open/closed positions with entries, SL/TP, trailing state |
| `orders` | Every order attempt with fill data + context JSON |
| `trades` | Closed trades with PnL + exit context JSON |
| `equity` | Snapshots every 5 minutes |
| `state` | Key-value store for resume timestamps etc. |

## Context JSON Schemas

### Order Context (`orders.context`)

Stored for every order — entries, exits, rejections.

```json
{
  "orderbook": {
    "mid_price": 49999.5,
    "expected_fill_price": 50000.1,
    "expected_slippage_pct": 0.0012,
    "spread_pct": 0.01,
    "depth_top5": {
      "bids": [[49999.0, 1.5], [49998.0, 3.2], ...],
      "asks": [[50000.0, 2.3], [50001.0, 1.8], ...]
    }
  },
  "signal": {
    "strength": 0.65,
    "confidence": 0.8,
    "direction": "long",
    "regime": "CALM_TRENDING",
    "strategy_id": "enhanced"
  },
  "market": {
    "candle_open": 49990.0,
    "candle_high": 50010.0,
    "candle_low": 49980.0,
    "candle_close": 50000.0,
    "candle_volume": 1234.5,
    "atr": 150.0
  },
  "execution": {
    "was_limit_attempted": true,
    "exchange_order_id": "abc123",
    "fill_latency_ms": 3200,
    "fell_back_to_market": false,
    "limit_wait_seconds": 3.2
  },
  "sizing": {
    "risk_per_trade": 0.03,
    "signal_risk": 0.65,
    "anti_martingale_streak": 3,
    "anti_martingale_mult": 1.3,
    "effective_risk": 0.025,
    "capital": 10200.0
  }
}
```

### Trade Context (`trades.context`)

Stored for every closed trade.

```json
{
  "position_state": {
    "highest_price_reached": 50600.0,
    "lowest_price_reached": null,
    "trailing_stop_level": 50500.0,
    "trailing_activated": true,
    "trailing_atr_multiplier": 2.0,
    "stop_loss": 49700.0,
    "take_profit": 50300.0
  },
  "exit_candle": {
    "open": 50550.0,
    "high": 50600.0,
    "low": 50400.0,
    "close": 50500.0,
    "volume": 567.8,
    "timestamp": "2026-05-04T10:05:00+00:00"
  },
  "atr_at_exit": 145.0,
  "anti_martingale_streak": 4,
  "capital_after": 10600.0
}
```

## Common Debug Queries

### Slippage Analysis

Are our limit orders actually saving money vs market orders?

```sql
-- Average slippage by symbol
SELECT
  symbol,
  ROUND(AVG(slippage_pct), 4) as avg_slippage_pct,
  ROUND(MIN(slippage_pct), 4) as best,
  ROUND(MAX(slippage_pct), 4) as worst,
  COUNT(*) as trades
FROM orders
WHERE status = 'filled' AND slippage_pct IS NOT NULL
GROUP BY symbol;

-- How often do we fall back from limit to market?
SELECT
  json_extract(context, '$.execution.fell_back_to_market') as used_market,
  COUNT(*) as count,
  ROUND(AVG(slippage_pct), 4) as avg_slippage
FROM orders
WHERE status = 'filled' AND context IS NOT NULL
GROUP BY used_market;

-- Fill latency distribution
SELECT
  symbol,
  ROUND(AVG(json_extract(context, '$.execution.fill_latency_ms')), 0) as avg_ms,
  ROUND(MAX(json_extract(context, '$.execution.fill_latency_ms')), 0) as max_ms,
  COUNT(*) as count
FROM orders
WHERE status = 'filled' AND context IS NOT NULL
GROUP BY symbol;

-- Slippage over time (daily averages)
SELECT
  date(created_at) as day,
  ROUND(AVG(slippage_pct), 4) as avg_slippage,
  COUNT(*) as orders
FROM orders
WHERE status = 'filled' AND slippage_pct IS NOT NULL
GROUP BY day
ORDER BY day;
```

### Orderbook Depth at Trade Time

Was there enough liquidity? Were we walking the book?

```sql
-- Spread at time of each order
SELECT
  symbol,
  created_at,
  side,
  order_type,
  ROUND(json_extract(context, '$.orderbook.spread_pct'), 4) as spread_pct,
  ROUND(json_extract(context, '$.orderbook.expected_slippage_pct'), 4) as expected_slippage,
  ROUND(slippage_pct, 4) as actual_slippage,
  ROUND(json_extract(context, '$.orderbook.mid_price'), 2) as mid_price,
  ROUND(avg_fill_price, 2) as fill_price
FROM orders
WHERE status = 'filled' AND context IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;

-- Full orderbook depth for a specific order (replace ORDER_ID)
SELECT json_extract(context, '$.orderbook.depth_top5') as depth
FROM orders
WHERE id = 'ORDER_ID';
```

### Signal Quality Analysis

Which signals were strong enough but turned out bad?

```sql
-- Signal strength vs actual PnL
SELECT
  t.symbol,
  t.side,
  t.exit_reason,
  ROUND(t.pnl, 2) as pnl,
  ROUND(json_extract(o.context, '$.signal.strength'), 2) as signal_strength,
  ROUND(json_extract(o.context, '$.signal.confidence'), 2) as signal_confidence,
  json_extract(o.context, '$.signal.regime') as regime,
  ROUND(json_extract(o.context, '$.market.atr'), 2) as atr_at_entry,
  ROUND(json_extract(o.context, '$.sizing.capital'), 2) as capital_at_entry
FROM trades t
JOIN orders o ON o.position_id = t.position_id AND o.reason = 'signal'
WHERE t.context IS NOT NULL AND o.context IS NOT NULL
ORDER BY t.pnl ASC
LIMIT 20;

-- Win rate by signal strength bucket
SELECT
  CASE
    WHEN json_extract(o.context, '$.signal.strength') >= 0.7 THEN 'strong (>=0.7)'
    WHEN json_extract(o.context, '$.signal.strength') >= 0.5 THEN 'medium (0.5-0.7)'
    ELSE 'weak (<0.5)'
  END as strength_bucket,
  COUNT(*) as trades,
  SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as wins,
  ROUND(AVG(t.pnl), 2) as avg_pnl
FROM trades t
JOIN orders o ON o.position_id = t.position_id AND o.reason = 'signal'
WHERE o.context IS NOT NULL
GROUP BY strength_bucket;
```

### Position Management Debugging

Why did trailing stop fire? How far did positions run?

```sql
-- Trailing stop details for closed trades
SELECT
  symbol,
  side,
  exit_reason,
  ROUND(pnl, 2) as pnl,
  ROUND(json_extract(context, '$.position_state.highest_price_reached'), 2) as highest,
  ROUND(exit_price, 2) as exit_price,
  ROUND(exit_price - json_extract(context, '$.position_state.highest_price_reached'), 2) as gap_from_high,
  json_extract(context, '$.position_state.trailing_activated') as trailing_on,
  ROUND(json_extract(context, '$.position_state.trailing_stop_level'), 2) as trail_level,
  duration_seconds / 60 as minutes_held
FROM trades
WHERE context IS NOT NULL
ORDER BY closed_at DESC
LIMIT 20;

-- Exit reason breakdown
SELECT
  exit_reason,
  COUNT(*) as count,
  ROUND(AVG(pnl), 2) as avg_pnl,
  ROUND(SUM(pnl), 2) as total_pnl,
  ROUND(AVG(duration_seconds / 60.0), 1) as avg_minutes
FROM trades
GROUP BY exit_reason;

-- Stop loss vs take profit hit rates
SELECT
  symbol,
  SUM(CASE WHEN exit_reason = 'stop_loss' THEN 1 ELSE 0 END) as sl_hits,
  SUM(CASE WHEN exit_reason = 'take_profit' THEN 1 ELSE 0 END) as tp_hits,
  SUM(CASE WHEN exit_reason = 'trailing_stop' THEN 1 ELSE 0 END) as trail_hits,
  COUNT(*) as total
FROM trades
GROUP BY symbol;
```

### ATR / Market Conditions

How does ATR at entry relate to trade outcomes?

```sql
-- ATR at entry vs trade PnL
SELECT
  symbol,
  ROUND(json_extract(o.context, '$.market.atr'), 2) as atr_entry,
  ROUND(json_extract(t.context, '$.atr_at_exit'), 2) as atr_exit,
  ROUND(t.pnl, 2) as pnl,
  t.exit_reason
FROM trades t
JOIN orders o ON o.position_id = t.position_id AND o.reason = 'signal'
WHERE o.context IS NOT NULL AND t.context IS NOT NULL
  AND json_extract(o.context, '$.market.atr') IS NOT NULL
ORDER BY t.closed_at DESC
LIMIT 20;

-- Candle that triggered the exit
SELECT
  symbol,
  exit_reason,
  ROUND(exit_price, 2) as exit_price,
  json_extract(context, '$.exit_candle') as exit_candle,
  ROUND(pnl, 2) as pnl
FROM trades
WHERE context IS NOT NULL
ORDER BY closed_at DESC LIMIT 10;
```

### Rejected / Failed Orders

Why didn't a trade happen?

```sql
-- All rejected orders
SELECT
  symbol, side, reason, created_at,
  json_extract(context, '$.signal') as signal,
  json_extract(context, '$.market') as market
FROM orders
WHERE status = 'rejected'
ORDER BY created_at DESC
LIMIT 20;

-- Error orders
SELECT symbol, side, reason, created_at
FROM orders
WHERE status = 'error'
ORDER BY created_at DESC;

-- Entry rejection reasons (from slippage guard)
SELECT
  symbol,
  created_at,
  json_extract(context, '$.orderbook.spread_pct') as spread_pct,
  json_extract(context, '$.orderbook.expected_slippage_pct') as expected_slippage
FROM orders
WHERE status = 'rejected' AND context IS NOT NULL
ORDER BY created_at DESC;
```

### Execution Quality

```sql
-- Limit vs market fill comparison
SELECT
  order_type,
  COUNT(*) as count,
  ROUND(AVG(slippage_pct), 4) as avg_slippage,
  ROUND(AVG(commission), 4) as avg_commission,
  ROUND(AVG(json_extract(context, '$.execution.fill_latency_ms')), 0) as avg_latency_ms
FROM orders
WHERE status = 'filled' AND context IS NOT NULL
GROUP BY order_type;

-- Partial fills
SELECT symbol, side, quantity, filled_quantity, order_type,
       ROUND(avg_fill_price, 2) as fill, created_at
FROM orders
WHERE status = 'partial'
ORDER BY created_at DESC;
```

### Equity & Performance

```sql
-- Equity curve (export for charting)
SELECT timestamp, total_equity, cash, unrealized_pnl, daily_pnl, total_pnl
FROM equity
ORDER BY id;

-- Daily PnL
SELECT
  date(timestamp) as day,
  ROUND(MAX(total_equity) - MIN(total_equity), 2) as range,
  ROUND(daily_pnl, 2) as daily_pnl,
  open_positions
FROM equity
GROUP BY day
ORDER BY day DESC
LIMIT 30;

-- Performance by symbol
SELECT
  symbol,
  COUNT(*) as trades,
  ROUND(SUM(pnl), 2) as total_pnl,
  ROUND(AVG(pnl), 2) as avg_pnl,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as win_pct,
  ROUND(SUM(commission), 2) as total_commission,
  ROUND(SUM(slippage), 2) as total_slippage,
  ROUND(AVG(duration_seconds / 60.0), 1) as avg_minutes
FROM trades
GROUP BY symbol
ORDER BY total_pnl DESC;
```

### Anti-Martingale Sizing

```sql
-- How sizing affected outcomes
SELECT
  symbol,
  ROUND(json_extract(o.context, '$.sizing.anti_martingale_mult'), 2) as size_mult,
  ROUND(json_extract(o.context, '$.sizing.effective_risk'), 4) as eff_risk,
  ROUND(t.pnl, 2) as pnl,
  t.exit_reason
FROM trades t
JOIN orders o ON o.position_id = t.position_id AND o.reason = 'signal'
WHERE o.context IS NOT NULL
ORDER BY t.closed_at DESC
LIMIT 30;
```

## Backtest Comparison

Compare live slippage against the backtest's 0.05% assumption:

```sql
-- Live vs backtest slippage
SELECT
  'live' as source,
  ROUND(AVG(slippage_pct), 4) as avg_slippage_pct
FROM orders
WHERE status = 'filled' AND slippage_pct IS NOT NULL
UNION ALL
SELECT
  'backtest' as source,
  0.05 as avg_slippage_pct;

-- Live PnL per trade vs backtest ($3.46 avg)
SELECT
  ROUND(AVG(pnl), 2) as avg_pnl,
  ROUND(AVG(commission), 2) as avg_commission,
  ROUND(AVG(slippage), 2) as avg_slippage_cost,
  COUNT(*) as total_trades
FROM trades;
```

## Export for Analysis

```bash
# Export all trades with context to CSV
sqlite3 -header -csv data/live_trading.db \
  "SELECT t.*, o.context as order_context FROM trades t LEFT JOIN orders o ON o.position_id = t.position_id AND o.reason = 'signal'" \
  > trades_export.csv

# Export equity curve
sqlite3 -header -csv data/live_trading.db \
  "SELECT * FROM equity ORDER BY id" \
  > equity.csv

# Pretty-print context JSON for a specific trade
sqlite3 data/live_trading.db \
  "SELECT json_pretty(context) FROM trades WHERE id = 'TRADE_ID'"
```
