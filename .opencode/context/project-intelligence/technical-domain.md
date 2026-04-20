<!-- Context: project-intelligence/technical | Priority: critical | Version: 1.0 | Updated: 2026-04-19 -->

# Technical Domain: Crypto Algo Trading Bot

**Purpose**: Tech stack, architecture, and development patterns for the crypto algo trading bot.
**Last Updated**: 2026-04-19

## Quick Reference
**Update Triggers**: Tech stack changes | New exchange adapters | Strategy modifications
**Audience**: Developers, AI agents

## Primary Stack
| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | 3.10+ | Async support, ML libraries |
| Env Manager | uv | latest | Fast Python venv creation |
| Framework | - | - | Modular, no heavy framework |
| Database | SQLite | - | Lightweight, async via aiosqlite |
| ML | PyTorch | 2.1+ | Signal prediction neural net |
| WebSocket | websockets + aiohttp | 12+ | Real-time market data |

## Architecture Pattern
```
Type: Modular async Python
Pattern: Exchange adapters → Signal generation → Order execution → Risk management
Layers: adapters/ | core/models/ | strategies/ | backtest/ | ml/
```

## Project Structure
```
src/
├── adapters/          # Exchange WebSocket/REST adapters (KuCoin, Bybit)
├── core/
│   ├── models/       # Data models (MarketData, Order, Position, Signal, Candle, Portfolio)
│   └── db/           # SQLite persistence layer
├── strategies/        # Trading strategies (momentum, mean reversion)
├── backtest/         # Historical testing engine with slippage modeling
├── ml/               # Neural net for signal prediction (features, model, training)
└── utils/            # Config, logging
config/               # YAML configuration files
```

## Key Technical Decisions
| Decision | Rationale | Impact |
|----------|-----------|--------|
| WebSocket-first | Real-time prices for signal generation | Lower latency than REST polling |
| uv for venv | Fast creation, modern Python management | Quick iteration |
| PyTorch for ML | Signal strength prediction from features | Better entry timing |
| Slippage modeling | Realistic backtest fills | Avoid overfitting |
| Pyramid entries | Multiple entries per position | Better avg price, reduced drawdown |

## Configuration Parameters

### MomentumConfig
| Parameter | Default | Description |
|-----------|---------|-------------|
| `risk_per_trade` | 3% | Position size as fraction of capital |
| `trailing_activation_atr` | 8.0 | ATR multiplier to activate trailing stop |
| `trailing_distance_atr` | 4.0 | ATR distance for trailing stop |
| `mtf_enabled` | True | Enable 1h H1Model LSTM confirmation |

### BacktestConfig
| Parameter | Default | Description |
|-----------|---------|-------------|
| `initial_capital` | $10,000 | Starting capital |
| `max_drawdown_pct` | 20% | Drawdown halt threshold |

## Backtest Results (60-day, 5m candles)
| Metric | Value |
|--------|-------|
| Win Rate | 100% |
| Total Return | 9.65% |
| Max Drawdown | 9.22% |
| Sharpe Ratio | 5,525 |
Settings: 3% risk, 8 ATR trailing activation, H1Model enabled.

## Bug Fixes Applied
1. **Drawdown calculation**: Fixed storing dollars as percentage
2. **Win rate calculation**: Fixed including open positions in win rate

## Backtest Scripts
| Script | Purpose |
|--------|---------|
| `h1_quick_test.py` | 7-day quick backtest |
| `balanced_60day_backtest.py` | Best performing 60-day test |
| `multi_asset_backtest.py` | Multi-asset backtest script |
| `trailing_stop_optimization.py` | Test ATR ranges |
| `kelly_backtest.py` | Kelly criterion position sizing |
| `meta_label_backtest.py` | Meta-labeling filter test |
| `regime_backtest.py` | Market regime detection |

## Multi-Asset Backtest Results (120-day, 5m candles)
| Symbol | Trades | Win Rate | TOTAL Return | Max DD |
|--------|--------|----------|--------------|--------|
| DOGEUSDT | 206 | 98.7% | **48.94%** | 10.31% |
| TONUSDT | 146 | 100% | **6.89%** | 10.94% |
| BTCUSDT | 65 | 100% | **0.59%** | 7.57% |
| TRXUSDT | 42 | 100% | **-6.59%** | 9.15% |

**Note**: XMR/USDT not available on Bybit or KuCoin.
**Finding**: High-volatility assets (DOGE) perform best with this strategy.

## Integration Points
| System | Purpose | Protocol |
|--------|---------|----------|
| KuCoin | Market data (tickers) | WebSocket |
| Bybit | Market data (tickers) | WebSocket |
| CCXT | Order execution (fallback) | REST |
| SQLite | Local persistence | aiosqlite |

## Code Patterns

### Exchange Adapter (WebSocket-first)
```python
class KuCoinAdapter:
    def __init__(self, config: Optional[KuCoinConfig] = None):
        self._price_cache: Dict[str, MarketData] = {}
        self._subscribers: list[Callable[[MarketData], None]] = []

    async def _connection_handler(self) -> None:
        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    await self._subscribe()
                    async for message in ws:
                        await self._handle_message(json.loads(message))
            except ConnectionClosed:
                await asyncio.sleep(self._reconnect_delay)
```

### Strategy Signal Generation
```python
class MomentumStrategy(BaseStrategy):
    async def generate_signal(self, symbol: str, candles: CandleSeries) -> Signal:
        rsi = self.calculate_rsi(candles.closes, 14)
        macd = self.calculate_macd(candles.closes)
        # Combine indicators → SignalDirection, strength, confidence
```

### Backtest with Slippage
```python
class FillSimulator:
    def calculate_fill_price(self, target_price: float, is_buy: bool, volume: float) -> float:
        slippage = self.slippage_factor * (1 + volume / self.avg_volume)
        return target_price * (1 + slippage) if is_buy else target_price * (1 - slippage)
```

## Naming Conventions
| Type | Convention | Example |
|------|-----------|---------|
| Files | snake_case | kucoin_adapter.py |
| Classes | PascalCase | KuCoinAdapter |
| Functions | snake_case | calculate_rsi |
| Constants | UPPER_SNAKE_CASE | MAX_RECONNECT_DELAY |
| Dataclasses | PascalCase | MarketData |
| Private | _prefix | _price_cache |

## Code Standards
- Type hints on all public functions
- Async/await for I/O operations
- Immutable dataclasses for data models (frozen=True where appropriate)
- Dependency injection via constructor
- Structured logging with structlog
- Pure functions where possible
- < 100 lines per module

## Security Requirements
- API keys via environment variables (never hardcoded)
- Exchange connections use HTTPS/WSS only
- Input validation on all external data
- Rate limiting on API calls
- No sensitive data in logs

## Development Environment
```
Setup: uv venv create && uv pip install -r pyproject.toml
Run: python -m src.main
Test: pytest tests/
```

## 📂 Codebase References
**Adapters**: src/adapters/kucoin_adapter.py, bybit_adapter.py
**Models**: src/core/models/market_data.py, order.py, position.py, signal.py, candle.py
**Strategies**: src/strategies/base_strategy.py, momentum_strategy.py, mean_reversion_strategy.py
**Backtest**: src/backtest/engine.py, fills.py, metrics.py
**ML**: src/ml/features.py, model.py, training.py
**Reference**: /home/dfr/orbitr/backend/src/services/websocket_market_service.py

## Related Files
- business-domain.md - Trading philosophy, risk approach
- orbitr reference: /home/dfr/orbitr (existing KuCoin/Bybit WebSocket implementations)