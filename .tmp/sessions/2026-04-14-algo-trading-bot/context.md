# Task Context: Algo Trading Bot for Crypto

Session ID: 2026-04-14-algo-trading-bot
Created: 2026-04-14T00:00:00Z
Status: in_progress

## Current Request
Build an algo trading bot for crypto markets using Python + uv, WebSocket-first APIs (KuCoin, Bybit), with backtesting, slippage/volume modeling, and neural net for signal prediction.

## Context Files (Standards to Follow)
- .opencode/context/core/standards/code-quality.md
- .opencode/context/development/principles/clean-code.md

## Reference Files (Source Material)
- /home/dfr/orbitr/backend/src/services/websocket_market_service.py - KuCoin/Bybit WebSocket implementations
- /home/dfr/orbitr/backend/src/services/exchange_service.py - CCXT wrapper for order execution
- KuCoin docs in .tmp/external-context/kucoin/

## External Docs Fetched
- KuCoin REST + WebSocket API documentation indexed

## Reddit Strategy Insights
- 2 parallel strategies with different volatility settings (regime adaptation)
- Pyramid 2 entries per position (second entry on dip)
- Max volatility filter to avoid extreme volatility entries
- Dynamic position sizing
- Books: Robert Carver, Ernest Chan

## Components (from TaskManager)
1. Project scaffold & config
2. Data models & schema
3. SQLite database layer
4. KuCoin WebSocket + REST
5. Bybit WebSocket + REST
6. Signal generation
7. Order execution + slippage modeling
8. Portfolio/position management
9. Dynamic position sizing
10. Base strategy class
11. Volatility filtering
12. Pyramid entry support
13. Parallel strategy runner
14. Historical replay engine (backtesting)
15. Realistic fill simulation (slippage/volume)
16. Performance metrics
17. Volume-based analysis
18. Feature engineering (neural net)
19. Neural net architecture
20. Training pipeline
21. Signal prediction
22. Integration tests
23. Example strategies

## Constraints
- Python + uv for venv management
- WebSocket-first for market data
- Leverage orbitr's existing KuCoin/Bybit implementations
- SQLite for persistence
- Neural net for signal enhancement
- Slippage modeling based on order book depth/volume
- Backtesting with realistic fill simulation

## Exit Criteria
- [ ] Project scaffold created with uv
- [ ] Exchange adapters (KuCoin/Bybit WebSocket + REST)
- [ ] Trading engine with slippage modeling
- [ ] Strategy framework with pyramid/parallel support
- [ ] Backtesting module with volume/slippage
- [ ] Neural net feature engineering + training
- [ ] Integration tests passing