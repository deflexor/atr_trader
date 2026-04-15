# Algo Trading Bot - Task Planning

## Feature Overview
**Objective**: Build a crypto algo trading bot with WebSocket market data, multi-strategy framework, backtesting, and neural net signal enhancement.

## Architecture

```
algo-trading-bot/
├── src/
│   ├── adapters/           # Exchange connections
│   │   ├── kucoin/        # KuCoin WebSocket + REST
│   │   └── bybit/         # Bybit WebSocket + REST
│   ├── core/              # Trading engine core
│   │   ├── models/        # Data models
│   │   ├── signals/       # Signal generation
│   │   ├── execution/    # Order execution
│   │   └── portfolio/    # Position management
│   ├── strategies/       # Strategy framework
│   │   ├── base.py       # Base strategy class
│   │   └── examples/     # Example strategies
│   ├── backtest/         # Backtesting engine
│   ├── ml/               # Neural net signal enhancement
│   └── utils/            # Utilities
├── tests/
├── config/               # Configuration files
└── data/                 # SQLite DB + historical data
```

## Subtask Plan

### Phase 1: Foundation
| Seq | Title | Dependencies | Parallel |
|-----|-------|--------------|----------|
| 01 | Project scaffold & config | - | true |
| 02 | Data models & schema | 01 | false |
| 03 | Database layer (SQLite) | 02 | false |

### Phase 2: Exchange Adapters
| Seq | Title | Dependencies | Parallel |
|-----|-------|--------------|----------|
| 04 | KuCoin WebSocket adapter | 01 | true |
| 05 | KuCoin REST adapter | 04 | false |
| 06 | Bybit WebSocket adapter | 01 | true |
| 07 | Bybit REST adapter | 06 | false |

### Phase 3: Trading Engine Core
| Seq | Title | Dependencies | Parallel |
|-----|-------|--------------|----------|
| 08 | Signal generation framework | 03 | false |
| 09 | Order execution with slippage | 03, 07 | false |
| 10 | Portfolio & position management | 09 | false |
| 11 | Dynamic position sizing | 10 | false |

### Phase 4: Strategy Framework
| Seq | Title | Dependencies | Parallel |
|-----|-------|--------------|----------|
| 12 | Base strategy class | 08 | false |
| 13 | Volatility filtering module | 12 | false |
| 14 | Pyramid entry support | 13 | false |
| 15 | Parallel strategy runner | 14 | false |

### Phase 5: Backtesting
| Seq | Title | Dependencies | Parallel |
|-----|-------|--------------|----------|
| 16 | Historical data replay engine | 03, 12 | false |
| 17 | Realistic fill simulation | 16 | false |
| 18 | Performance metrics (Sharpe, drawdown) | 17 | false |
| 19 | Volume-based analysis | 17 | false |

### Phase 6: Neural Net Signal Enhancement
| Seq | Title | Dependencies | Parallel |
|-----|-------|--------------|----------|
| 20 | Feature engineering pipeline | 03 | true |
| 21 | Neural net model architecture | 20 | false |
| 22 | Training pipeline | 21 | false |
| 23 | Signal strength prediction | 22 | false |

### Phase 7: Integration & Testing
| Seq | Title | Dependencies | Parallel |
|-----|-------|--------------|----------|
| 24 | Integration tests | 11, 15, 18, 23 | false |
| 25 | Example strategy (Carver/Chan inspired) | 15 | true |

## Exit Criteria
- All tests passing
- Backtesting produces realistic results
- Neural net can be trained and produces predictions
- Example strategies demonstrate pyramid/parallel capabilities
