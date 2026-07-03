# Wheel Strategy Signal System — Architecture Plan

## Context
A personal, signal-only AI trading system implementing the Wheel options strategy on US large-cap equities.
Phase 1 goal: produce structured trade signal cards. No live execution in Phase 1.
Claude Code + MCP is the preferred tooling stack. Research was conducted during this planning session to decide the broker/data source, which was open at session start.

---

## 1. Broker & Data Source Decision

### Research Summary (April 2026)

| Provider | MCP Status | Greeks | IV | Paper Trade | Auth | Notes |
|---|---|---|---|---|---|---|
| **Alpaca** | ✅ Official v2 (Apr 2025) | ✅ Full | ✅ Yes | ✅ Yes | API Key | 61 endpoints; options chains + snapshots |
| **Tradier** | ✅ Official hosted (Dec 2025) | ✅ Via API | ✅ Yes | ✅ Yes | API Token | Hosted, zero infra; 15-min delayed free |
| **Tastytrade** | ✅ Community (`tastytrade-mcp`) | ✅ Full | ✅ Built-in | ✅ Yes | Username | Options-first; IV analysis built in |
| **IBKR** | ✅ Community (multiple repos) | ✅ Full | ⚠️ Via TWS | ✅ Yes | TWS/Gateway | Requires running TWS locally |
| **Schwab/TD** | ⚠️ Community only | ⚠️ Limited | ⚠️ Limited | ⚠️ Limited | OAuth | Not officially supported |
| **TradingView** | ⚠️ Analysis only | N/A | N/A | N/A | — | Signal gen only; not a broker |
| **CBOE** | ❌ None | ✅ Delayed | ✅ Delayed | N/A | None | Free HTTP endpoint; ~15-min delay; no auth |
| **Polygon.io** | ❌ None | ✅ Full | ✅ Full | N/A | API Key | Free tier: 5 calls/min, EOD only |

### Fallback Data Sources (Phase 1 / No Account)
- **CBOE delayed quotes**: Free HTTP endpoint; no auth required. Suitable for Phase 1 fixture-based testing.
- **Polygon.io free tier**: Requires API key (free signup); EOD options data with Greeks.
- **Tradier sandbox**: Free signup; 15-min delayed options chains + Greeks; full API parity with live.

### RECOMMENDED: **Alpaca**
**Reason**: Only broker with an official, Anthropic-MCP-compatible server (v2, April 2025) exposing all 61 endpoints including full options chains with delta/gamma/theta/vega/rho, implied volatility, real-time quotes, and account state — all under a single free-tier API key with paper trading support. The MCP server is maintained by Alpaca itself, reducing the risk of breakage. Tradier is the first-ranked fallback because it also has an officially maintained hosted MCP server and generous free tier.

**Phase 1 note**: Alpaca paper trading API is used from Phase 1 onwards. The paper trading environment provides full options chains, Greeks, and live market data with zero financial risk. Credentials are identical to a live Alpaca account but routed to Alpaca's paper trading base URL (`https://paper-api.alpaca.markets`). This removes the need for a Tradier sandbox account and keeps the data adapter consistent across all phases. Tradier remains the documented fallback if Alpaca access is unavailable.

---

## 2. System Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        WHEEL SIGNAL SYSTEM                          │
│                                                                     │
│  ┌─────────────┐    ┌──────────────────┐    ┌───────────────────┐  │
│  │  DATA       │    │  INDICATOR       │    │  SIGNAL ENGINE    │  │
│  │  INGESTION  │───▶│  COMPUTATION     │───▶│  (orchestrator)   │  │
│  │             │    │                  │    │                   │  │
│  │ • OHLCV bars│    │ • 50-EMA + slope │    │  loads strategy   │  │
│  │ • Options   │    │ • RSI(14)        │    │  calls evaluate() │  │
│  │   chains    │    │ • BB(20,2)       │    └────────┬──────────┘  │
│  │ • Volume    │    │ • Volume profile │             │              │
│  │   history   │    │ • Delta lookup   │             ▼              │
│  └─────────────┘    └──────────────────┘    ┌───────────────────┐  │
│       ▲                                     │  STRATEGY LAYER   │  │
│       │                                     │                   │  │
│  ┌────┴──────┐                              │  Strategy Protocol│  │
│  │  MCP /    │                              │  ┌─────────────┐  │  │
│  │  REST     │                              │  │WheelStrategy│  │  │
│  │  ADAPTER  │                              │  │  filters.py │  │  │
│  │           │                              │  │  state.py   │  │  │
│  │  Alpaca   │                              │  └─────────────┘  │  │
│  │  MCP v2   │                              │  (+ future        │  │
│  │  (or      │                              │   strategies)     │  │
│  │  Tradier  │                              └────────┬──────────┘  │
│  │  sandbox) │                                       │              │
│  └───────────┘     ┌──────────────────┐             ▼              │
│                    │  STATE STORE     │    ┌───────────────────┐   │
│                    │  (strategy-owned)│    │  SIGNAL OUTPUT    │   │
│                    │                  │    │  LAYER            │   │
│                    │  wheel_state.json│    │                   │   │
│                    │  per-ticker:     │    │ • JSON cards      │   │
│                    │  • phase (A/B)   │    │ • Rich terminal   │   │
│                    │  • cost_basis    │    │ • JSONL log       │   │
│                    │  • shares_held   │    └───────────────────┘   │
│                    └──────────────────┘                            │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  FUTURE: EXECUTION LAYER (Phase 3+)                         │   │
│  │  Alpaca MCP → order placement via Claude tool calls         │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Specifications

| Component | Inputs | Outputs | Technology | Why |
|---|---|---|---|---|
| **MCP/REST Adapter** | Ticker list, date | OHLCV bars, options chains, Greeks | `alpaca-py` SDK or `requests` (Tradier) | Abstracts broker; swappable via config |
| **Data Ingestion** | Raw API responses | Normalized DataFrames (OHLCV, options chain) | `pandas`, `alpaca-py` | Standard DataFrame pipeline |
| **Indicator Computation** | OHLCV DataFrame | EMA series, RSI, BB bands, volume nodes | `pandas-ta` | Pure-Python, no C deps; covers all 4 indicators |
| **Signal Engine** | Indicator values, options chain, active strategy | Raw signal + confidence | Pure Python orchestrator (`signal_engine.py`) | Thin dispatcher; all conditional logic lives in the strategy |
| **Strategy Layer** | Market data dict from signal engine | Signal (phase, strike, confidence, flags) | `Strategy` Protocol + concrete implementations | Encapsulates filter chain + state schema per strategy; swappable without touching infrastructure |
| **State Store** | Assignment events, strategy name | Current state per ticker | `{strategy}_state.json` (local file, read/write) | Simple, portable; namespaced per strategy; no DB overhead for Phase 1 |
| **Signal Output Layer** | Raw signal | Formatted signal card | `pydantic` (schema) + `rich` (terminal) | Typed output; pretty-prints to terminal and JSONL |
| **Scheduler** | Config (tickers, times) | Triggers pipeline | `APScheduler` | Lightweight; runs in-process; no Redis needed |

### Claude Code vs Script vs MCP Breakdown

- **Claude Code tasks**: Interpreting signal cards, generating thesis_text, resolving edge cases, running ad-hoc signal checks interactively
- **Standalone scripts**: `run_signals.py` (daily batch), `backfill_indicators.py` (historical data)
- **MCP tool calls**: `alpaca-mcp` for options chain fetch and (Phase 3) order placement

---

## 3. Strategy Plugin Architecture

The `DataAdapter` Protocol makes claude-trader broker-agnostic. The same abstraction is now extended to **trading strategies**: the `Strategy` Protocol makes `signal_engine.py` strategy-agnostic. A strategy encapsulates its own filter chain, state schema, and evaluation logic — the signal engine becomes a thin orchestrator that simply loads a strategy and calls `evaluate()`. This mirrors the DataAdapter pattern exactly.

### 3.1 Strategy Protocol

```python
# src/strategies/base.py
from typing import Protocol, List, Any, runtime_checkable
from pydantic import BaseModel

class FilterResult(BaseModel):
    """Typed result returned by every Filter.
    Enables WheelStrategy to collect all results and apply hard stops + soft
    adjustments in a single second pass rather than returning early mid-chain.
    Also included verbatim in the emitted Signal as `filter_trace`."""
    passed: bool
    filter_name: str
    reason: str
    hard_stop: bool                    # True → evaluation halts after this filter; no further filters run
    adjustments: dict[str, Any] = {}   # soft adjustments e.g. {"confidence_penalty": -1, "strike_override": 182.5}

class Filter(Protocol):
    """A discrete, testable filter step within a strategy."""
    name: str
    def evaluate(self, market_data: dict) -> FilterResult: ...

@runtime_checkable
class Strategy(Protocol):
    """Contract every strategy must satisfy."""
    name: str                                               # e.g. "wheel"
    def load_config(self, path: str) -> None: ...          # reads config/{name}.toml at startup; each strategy defines its own TOML
    def get_filters(self) -> List[Filter]: ...             # ordered filter chain
    def evaluate(self, market_data: dict) -> Signal: ...   # run full pipeline → collects FilterResults; second pass applies hard stops then soft adjustments
    def get_state_schema(self) -> type[BaseModel]: ...     # Pydantic model for this strategy's state
```

`signal_engine.py` becomes strategy-agnostic: it resolves the active strategy from the registry, assembles `market_data`, and calls `strategy.evaluate(market_data)`. All Wheel-specific logic moves entirely into `src/strategies/wheel/`.

### 3.2 WheelStrategy — First Concrete Implementation

The current 7-step filter chain in `signal_engine.py` is refactored into three focused files under `src/strategies/wheel/`:

| File | Responsibility |
|---|---|
| `strategy.py` | `WheelStrategy` class implementing the `Strategy` Protocol; calls `load_config("config/wheel.toml")` at startup; runs the filter chain and collects all `FilterResult` objects; second pass applies hard stops first, then accumulates soft adjustments (confidence penalties, strike overrides) from `adjustments`; assembles and returns the `Signal` |
| `filters.py` | Seven discrete `Filter` classes: `PhaseGateFilter`, `EMAFilter`, `RSIFilter`, `BBFilter`, `OptionsChainFilter`, `DeltaFilter`, `VolumeProfileFilter` — each returns a `FilterResult` Pydantic model (never raises or returns early); each independently testable |
| `state.py` | `WheelState` Pydantic model: `phase`, `shares_held`, `cost_basis`, `assignment_date`, `open_put_strike`, `open_put_expiry` |

`signal_engine.py` shrinks to ~20 lines: resolve strategy from registry → call `strategy.evaluate(market_data)` → return signal. The Phase A/B decision tree, filter precedence rules, and confidence tier logic all remain intact — they simply live inside `WheelStrategy` rather than directly in `signal_engine.py`.

### 3.3 Strategy Registry & CLI Flag

```python
# src/strategies/registry.py
from src.strategies.wheel.strategy import WheelStrategy

STRATEGY_REGISTRY: dict[str, type] = {
    "wheel": WheelStrategy,
}

def get_strategy(name: str) -> Strategy:
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy '{name}'. Available: {list(STRATEGY_REGISTRY)}")
    return STRATEGY_REGISTRY[name]()
```

`run_signals.py` accepts a `--strategy` flag (default: `wheel`):

```
python run_signals.py --tickers AAPL MSFT --strategy wheel
```

Adding a new strategy requires touching only `registry.py` — no other infrastructure files change.

### 3.4 Strategy-Owned State

Each strategy defines its own Pydantic state schema via `get_state_schema()`. State files are namespaced by strategy name so that multiple strategies can run against the same ticker universe without state collisions.

| Before | After |
|---|---|
| `phase_state.json` | `wheel_state.json` |
| `phase_state.py` (Wheel-specific read/write helpers) | `state_store.py` (generic; accepts any strategy name + schema) |

`state_store.py` API:

```python
def load_state(strategy_name: str, schema: type[BaseModel]) -> dict[str, BaseModel]: ...
def save_state(strategy_name: str, state: dict[str, BaseModel]) -> None: ...
```

State files live at `{strategy_name}_state.json` in the project root (e.g. `wheel_state.json`). Each new strategy automatically gets its own isolated state file — no changes to `state_store.py` required.

### 3.5 Updated File Structure

```
src/
  strategies/
    base.py              ← Strategy Protocol + Filter Protocol + FilterResult
    registry.py          ← maps name → class; only file touched when adding a strategy
    wheel/
      __init__.py
      strategy.py        ← WheelStrategy: implements Strategy Protocol;
                            orchestrates filter chain; returns Signal
      filters.py         ← PhaseGateFilter, EMAFilter, RSIFilter, BBFilter,
                            OptionsChainFilter, DeltaFilter, VolumeProfileFilter
      state.py           ← WheelState Pydantic model (phase, cost_basis,
                            shares_held, assignment_date, open positions)
  adapters/
    base.py              ← DataAdapter Protocol (unchanged)
    alpaca.py            ← AlpacaPaperAdapter (unchanged)
    fixture.py           ← FixtureAdapter (unchanged)
  indicator_engine.py    ← shared across all strategies (unchanged)
  signal_engine.py       ← thin orchestrator: load strategy → call evaluate() (~20 lines)
  signal_output.py       ← unchanged
  state_store.py         ← renamed from phase_state.py; generic state read/write
  logger.py              ← structlog setup; JSON renderer → logs/signals.log (newline-delimited JSON);
                            ConsoleRenderer for terminal; imported once at startup

config/
  watchlist.toml         ← unchanged
  wheel.toml             ← WheelStrategy filter parameters loaded at startup via load_config();
                            keys: ema_period, rsi_min, rsi_max, delta_target, delta_tolerance,
                            dte_min, dte_max, bb_period, bb_std_dev, volume_lookback_bars,
                            soft_flag_confidence_penalty
                            (future strategies each define their own config TOML here)
fixtures/                ← unchanged (fixture JSON files for offline tests)
logs/                    ← runtime log output; in .gitignore
  signals.log            ← structlog JSON-lines output (filter traces, signal decisions);
                            separate from signals.jsonl which is the structured signal card log
run_signals.py           ← --strategy flag added (default: wheel); --adapter flag unchanged
wheel_state.json         ← renamed from phase_state.json; WheelState per ticker
```

---

## 4. Signal Pipeline Design

### Filter Execution Order (cheapest-first)

```
For each ticker in universe:

  STEP 1: Phase Gate [O(1), no I/O]
  ├── Read wheel_state.json for this ticker
  ├── Phase A → evaluate PUT signal path
  └── Phase B → evaluate CALL signal path

  STEP 2: Price + EMA Trend Filter [O(n), local compute]
  ├── Fetch last 60 OHLCV bars (or use cached)
  ├── Compute 50-EMA using pandas-ta
  ├── Check: price > EMA50 AND slope(EMA50, last 5 bars) > 0
  └── FAIL → emit NO_SIGNAL("ema_trend_fail"); STOP this ticker

  STEP 3: RSI Gate [O(n), local compute]
  ├── Compute RSI(14) using pandas-ta on same bars
  ├── Check: 35 ≤ RSI ≤ 65
  └── FAIL → emit NO_SIGNAL("rsi_out_of_range"); STOP this ticker

  STEP 4: Bollinger Band Positioning [O(n), local compute]
  ├── Compute BB(20, 2) using pandas-ta
  ├── Phase A (put sell): prefer price ≤ BB_lower + 0.2 * (BB_upper - BB_lower)
  │   └── SOFT filter: flag bb_position = "unfavorable" but do not hard-stop
  └── Phase B (call sell): prefer price ≥ BB_upper - 0.2 * (BB_upper - BB_lower)
      └── SOFT filter: flag bb_position = "unfavorable" but do not hard-stop

  STEP 5: Options Chain Fetch [I/O, expensive]
  ├── Call Alpaca MCP / Tradier API for nearest expiry in 30–45 DTE window
  ├── Retrieve full chain with delta, IV for all strikes
  └── FAIL (no chain, no liquid options) → emit NO_SIGNAL("no_options_data"); STOP

  STEP 6: Delta Targeting [O(k), chain scan]
  ├── Phase A: Find put strike nearest to delta = -0.30 (±0.05 tolerance)
  ├── Phase B: Find call strike nearest to delta = +0.30 (±0.05 tolerance)
  └── FAIL (no strike in tolerance band) → emit NO_SIGNAL("no_delta_match"); STOP

  STEP 7: Volume Profile Check [O(n), local compute]
  ├── Compute volume distribution across last 20 bars using pandas-ta VP or custom histogram
  ├── Identify highest-volume price node (HVN) = support/resistance
  ├── Phase A: Recommended strike should be BELOW nearest HVN (put support anchor)
  └── Phase B: Recommended strike should be NEAR prior HVN ceiling (call resistance)
      └── SOFT filter: adjust strike recommendation; flag if diverging >5% from delta target

  EMIT SIGNAL:
  └── Assemble signal card (see Section 5)
```

### Decision Tree

```
                         START
                           │
              ┌────────────▼────────────┐
              │   Read phase_state      │
              └────────────┬────────────┘
                  ┌────────┴────────┐
                Phase A           Phase B
              (No shares)       (100 shares held)
                  │                  │
        ┌─────────▼──────┐  ┌────────▼──────────┐
        │ EMA trend ✓?   │  │ EMA trend ✓?       │
        └────┬───────┬───┘  └────┬───────┬───────┘
            YES      NO         YES      NO
             │       │           │       │
             │   NO_SIGNAL       │   NO_SIGNAL
             │                   │
        ┌────▼────┐         ┌────▼────┐
        │ RSI ok? │         │ RSI ok? │
        └────┬────┘         └────┬────┘
            YES                  YES
             │                   │
        ┌────▼──────────┐   ┌────▼──────────────┐
        │ BB near lower?│   │ BB near upper?     │
        │ (soft check)  │   │ (soft check)       │
        └────┬──────────┘   └────┬───────────────┘
             │                   │
        ┌────▼──────┐       ┌────▼──────────┐
        │ Options   │       │ Options chain  │
        │ chain +   │       │ + delta target │
        │ delta -30 │       │ delta +30      │
        └────┬──────┘       └────┬───────────┘
             │                   │
        ┌────▼──────────┐   ┌────▼───────────────┐
        │ Vol profile   │   │ Vol profile anchor  │
        │ anchor below  │   │ near HVN ceiling    │
        │ HVN support   │   └────┬────────────────┘
        └────┬──────────┘        │
             │                   │
        SELL_PUT signal    SELL_CALL signal
        card emitted       card emitted
```

### Phase Transition Mechanics

**State tracked per ticker in `wheel_state.json`:**
```json
{
  "AAPL": {
    "phase": "A",
    "shares_held": 0,
    "cost_basis": null,
    "assignment_date": null,
    "open_put_strike": null,
    "open_put_expiry": null
  }
}
```

**Phase A → Phase B trigger**: User manually updates `wheel_state.json` when put is assigned (Phase 1 is signal-only; no execution API). Phase 3 will auto-update via assignment event webhook.

**Phase B → Phase A trigger**: User manually updates when covered call is exercised (shares called away).

**Conflict Resolution Rules (strict precedence):**

1. **50-EMA declining → HARD STOP.** All other indicators are overridden. RSI in range and BB at lower band do not override a declining EMA. Rationale: trending down means the mean-reversion put-sell thesis is invalid.
2. **RSI out of range (>65 or <35) → HARD STOP.** Overbought/oversold conditions signal extremes not appropriate for wheel entry.
3. **BB position unfavorable → SOFT FLAG only.** Lowers confidence tier from HIGH → MEDIUM. Does not block signal.
4. **Volume node divergence >5% from delta target → SOFT FLAG only.** Noted in risk_flags; does not block signal.

---

## 5. Signal Output Schema

### JSON Schema

```json
{
  "ticker": "string",
  "phase": "SELL_PUT | SELL_CALL | NO_SIGNAL",
  "signal_timestamp": "ISO-8601 datetime (UTC)",
  "underlying_price": "float",
  "recommended_strike": "float",
  "recommended_expiry": "YYYY-MM-DD",
  "dte": "integer (days to expiry)",
  "delta_estimate": "float (negative for puts, positive for calls)",
  "iv_rank": "float (0–100, IV percentile, optional)",
  "indicator_readings": {
    "ema50": "float",
    "ema50_slope": "float (5-bar slope, positive = rising)",
    "rsi14": "float",
    "bb_upper": "float",
    "bb_lower": "float",
    "bb_percent_b": "float (0 = at lower band, 1 = at upper band)",
    "volume_node_nearest": "float (price level of nearest HVN)",
    "volume_node_type": "support | resistance"
  },
  "thesis_text": "string (one sentence, Claude-generated)",
  "confidence_tier": "HIGH | MEDIUM | LOW",
  "confidence_rationale": "string",
  "risk_flags": ["string"],
  "no_signal_reason": "string | null (populated only when phase = NO_SIGNAL)",
  "filter_trace": [
    {
      "filter_name": "string",
      "passed": "bool",
      "reason": "string",
      "hard_stop": "bool",
      "adjustments": "dict[str, Any] — e.g. {\"confidence_penalty\": -1} or {\"strike_override\": 182.5}"
    }
  ]
}
```

### Confidence Tier Thresholds

| Tier | Definition |
|---|---|
| **HIGH** | All 6 filters pass cleanly; BB position favorable; volume node confirms strike; delta within ±0.02 of target |
| **MEDIUM** | Core filters pass (EMA + RSI); ≤1 soft flag tripped (BB unfavorable OR vol node diverges); delta within ±0.05 |
| **LOW** | Core filters pass; 2+ soft flags; or delta at edge of tolerance (±0.05 to ±0.08); or IV rank missing |

### Worked Example (Fictional Ticker: WXYZ)

```json
{
  "ticker": "WXYZ",
  "phase": "SELL_PUT",
  "signal_timestamp": "2026-04-16T14:35:00Z",
  "underlying_price": 187.42,
  "recommended_strike": 180.0,
  "recommended_expiry": "2026-05-16",
  "dte": 30,
  "delta_estimate": -0.29,
  "iv_rank": 42.1,
  "indicator_readings": {
    "ema50": 182.10,
    "ema50_slope": 0.34,
    "rsi14": 48.7,
    "bb_upper": 195.20,
    "bb_lower": 181.30,
    "bb_percent_b": 0.44,
    "volume_node_nearest": 182.50,
    "volume_node_type": "support"
  },
  "thesis_text": "WXYZ is in an uptrend above a rising 50-EMA with neutral RSI; the $180 put sits below the high-volume support node at $182.50, offering a defined-risk income entry.",
  "confidence_tier": "HIGH",
  "confidence_rationale": "All 6 filters passed; BB position neutral; volume node confirms strike placement below support.",
  "risk_flags": [],
  "no_signal_reason": null,
  "filter_trace": [
    {"filter_name": "PhaseGateFilter",     "passed": true,  "reason": "Phase A — no open position",            "hard_stop": false, "adjustments": {}},
    {"filter_name": "EMAFilter",           "passed": true,  "reason": "Price 187.42 > EMA50 182.10; slope +0.34", "hard_stop": false, "adjustments": {}},
    {"filter_name": "RSIFilter",           "passed": true,  "reason": "RSI 48.7 within [35, 65]",              "hard_stop": false, "adjustments": {}},
    {"filter_name": "BBFilter",            "passed": true,  "reason": "BB %B 0.44 — neutral positioning",      "hard_stop": false, "adjustments": {}},
    {"filter_name": "OptionsChainFilter",  "passed": true,  "reason": "Chain found; 30 DTE; IV rank 42.1",     "hard_stop": false, "adjustments": {}},
    {"filter_name": "DeltaFilter",         "passed": true,  "reason": "Put delta -0.29 within target -0.30 ±0.05", "hard_stop": false, "adjustments": {}},
    {"filter_name": "VolumeProfileFilter", "passed": true,  "reason": "Strike 180.0 below HVN support at 182.50", "hard_stop": false, "adjustments": {}}
  ]
}
```

---

## 6. Technical Stack Specification

### Language & Runtime
- **Python 3.11+** (for `tomllib` stdlib, `match` statements, improved typing)
- **Synchronous** in Phase 1–2 (simplicity); async upgrade considered in Phase 3 if parallel ticker scanning becomes a bottleneck
- **Virtual environment**: `venv` (standard library); `pyproject.toml` for dependency declaration

### Libraries

| Category | Library | Version | Purpose |
|---|---|---|---|
| Options data | `alpaca-py` | ≥0.13 | Options chains, Greeks, quotes via Alpaca MCP/REST |
| Options data (fallback) | `requests` | stdlib-compatible | Tradier sandbox REST calls |
| Technical indicators | `pandas-ta` | latest stable | EMA, RSI, Bollinger Bands, VWAP, volume profile |
| Greeks calculation | `py_vollib` | 1.0.3 | Black-Scholes Greeks from price/IV (offline verification) |
| Schema / validation | `pydantic` v2 | ≥2.0 | Typed signal cards; JSON serialization; input validation |
| Scheduling | `APScheduler` | ≥3.10 | Daily market-open trigger; in-process; no Redis needed |
| Terminal output | `rich` | latest | Formatted signal card display; color-coded confidence tiers |
| Template output | `jinja2` | latest | Optional human-readable signal summary templating |
| Data manipulation | `pandas` | ≥2.0 | DataFrame pipeline throughout |
| Config | `python-dotenv` | latest | Loads `.env` secrets; standard pattern |
| Structured logging | `structlog` | ≥24.0 | JSON renderer for `logs/signals.log`; ConsoleRenderer for terminal; every filter step and signal decision emits a structured log event |

### MCP Integration Pattern

```
Claude Code session
       │
       │  calls tool: alpaca_get_options_chain(ticker, dte_min=30, dte_max=45)
       ▼
Alpaca MCP Server v2 (local process, stdio transport)
       │
       │  HTTP to Alpaca REST API (authenticated)
       ▼
Alpaca API → returns options snapshot JSON → back to Claude Code tool result
       │
Claude Code parses result → passes to signal_engine.py via subprocess or direct Python import
```

The MCP adapter is abstracted behind a `DataAdapter` Protocol so Tradier sandbox can be hot-swapped:

```python
# src/adapters/base.py
class DataAdapter(Protocol):
    def get_ohlcv(self, ticker: str, bars: int) -> pd.DataFrame: ...
    def get_options_chain(self, ticker: str, dte_min: int, dte_max: int) -> pd.DataFrame: ...
```

### Local vs Hosted
- **Phase 1–2**: Runs entirely on local machine, triggered manually or by APScheduler
- **Phase 3+**: Consider lightweight VPS (e.g., $6/mo DigitalOcean droplet) if 24/7 scheduling needed
- **Claude Code session**: Used for interactive signal review and ad-hoc runs; not the primary scheduler

### Secrets Management
- All API keys stored in `.env` file at project root
- `.env` added to `.gitignore` immediately
- `logs/` added to `.gitignore` (runtime log output; not version-controlled)
- Loaded via `python-dotenv` at startup
- No secrets in source code or signal output files
- `.env.example` committed to version control with empty placeholder values (keys present, values blank)

```
# .env  ← never committed; copy from .env.example and fill in values
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # switch to https://api.alpaca.markets for live
TRADIER_API_TOKEN=...   # fallback only
POLYGON_API_KEY=...     # optional, Phase 1 fixture testing only
```

### Alpaca API Credentials

**Where to obtain:**
1. Log in at [app.alpaca.markets](https://app.alpaca.markets)
2. Navigate to **Paper Trading** (top-left environment toggle)
3. Go to **Settings → API Keys → Generate New Key**
4. Copy the **API Key ID** (`ALPACA_API_KEY`) and **Secret Key** (`ALPACA_SECRET_KEY`) — the secret is shown only once

**How they are used:**
- `alpaca-py` SDK reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` from environment at startup via `python-dotenv`
- `ALPACA_BASE_URL` controls paper vs live routing — always `https://paper-api.alpaca.markets` in Phase 1–3
- Keys are **never hardcoded** in any source file; all credential access goes through `os.environ` / `python-dotenv`
- The Alpaca MCP v2 server also reads these from the environment when spawned by Claude Code

**Paper trading scope (Phase 1–3):** Full options chain snapshots, OHLCV bars, Greeks (delta/gamma/theta/vega/rho), IV, and account state are all available under paper credentials. No real money is at risk. Switching to live trading in Phase 4 requires only changing `ALPACA_BASE_URL`.

---

## 7. Implementation Phases

### Phase 1 — Live Pipeline with Alpaca Paper Trading

**Data source:** Alpaca paper trading API (`https://paper-api.alpaca.markets`) via `alpaca-py` SDK. Credentials loaded from `.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`). A `FixtureAdapter` is also built for offline unit testing (loads from `fixtures/` JSON files), but real runs use `AlpacaPaperAdapter`.

**What it builds:**
- Project scaffold: `pyproject.toml`, `src/` layout, `.env.example`, `.gitignore`
- `DataAdapter` interface + `FixtureAdapter` (loads from `fixtures/` JSON files for offline tests)
- `AlpacaPaperAdapter`: implements `DataAdapter` using `alpaca-py`; fetches OHLCV bars via `StockHistoricalDataClient` and options chains via `OptionHistoricalDataClient`; routes all requests to `ALPACA_BASE_URL` (paper endpoint)
- `indicator_engine.py`: computes EMA50, RSI14, BB(20,2), volume profile from OHLCV DataFrame
- `src/strategies/base.py`: `Strategy` Protocol + `Filter` Protocol + `FilterResult`
- `src/strategies/registry.py`: strategy name → class mapping; `get_strategy(name)` helper
- `src/strategies/wheel/strategy.py`: `WheelStrategy` implementing the `Strategy` Protocol; full filter chain + confidence tier logic migrated from `signal_engine.py`
- `src/strategies/wheel/filters.py`: seven discrete `Filter` classes, each independently unit-testable
- `src/strategies/wheel/state.py`: `WheelState` Pydantic model (Phase A/B, positions)
- `signal_engine.py`: thin orchestrator (~20 lines) — resolves strategy from registry, assembles market data, calls `strategy.evaluate()`
- `state_store.py`: generic state read/write helpers accepting any strategy name + schema; replaces `phase_state.py`
- `wheel_state.json`: Wheel strategy state file (replaces `phase_state.json`)
- `signal_output.py`: Pydantic schema + `rich` terminal formatter + JSONL logger
- `run_signals.py`: CLI entry point (`--tickers AAPL MSFT --strategy wheel --adapter alpaca|fixture`)
- `config/wheel.toml`: WheelStrategy config file; `WheelStrategy.load_config("config/wheel.toml")` is called at startup; all filter parameters (ema_period, rsi_min, rsi_max, delta_target, delta_tolerance, dte_min, dte_max, bb_period, bb_std_dev, volume_lookback_bars, soft_flag_confidence_penalty) read from this file rather than hardcoded
- `src/logger.py`: structlog setup module; called once at application startup; configures JSON renderer writing to `logs/signals.log` (newline-delimited JSON) and ConsoleRenderer for terminal output; every filter step logs `filter_name`, `passed`, `reason`, and key indicator values; the signal engine logs `strategy_name`, `ticker`, `total_filters_run`, `final_signal_type`, `confidence_tier`

**Fixture data:** 3–5 pre-saved OHLCV + options chain JSON files in `fixtures/` for deterministic unit tests: clear PUT signal, clear CALL signal, EMA fail, RSI fail, LOW confidence (soft flags). These do not require credentials.

**Test criterion:**
- `pytest tests/unit/strategies/test_strategy_protocol.py` — verifies `WheelStrategy` satisfies the `Strategy` Protocol (runtime_checkable); verifies `get_filters()` returns correct count and order; verifies `get_state_schema()` returns a `BaseModel` subclass; verifies `load_config("config/wheel.toml")` populates all 11 expected parameters
- `pytest tests/unit/strategies/wheel/` — tests each of the 7 `Filter` classes in isolation against fixture market data; each test asserts the returned `FilterResult` is a Pydantic model instance with correct `filter_name`, `passed`, `reason`, `hard_stop`, and `adjustments` for that fixture scenario; tests `WheelStrategy.evaluate()` end-to-end against all 5 fixture scenarios; asserts `filter_trace` in emitted signal card contains one `FilterResult` per filter run; asserts hard-stop filters (EMA, RSI) produce `hard_stop=True` and that subsequent filters are not evaluated; asserts soft-flag filters (BB, VolumeProfile) produce `hard_stop=False` with correct `adjustments` (e.g. `confidence_penalty`)
- `pytest tests/unit/test_indicator_engine.py` — all 4 indicator values verified against manually computed reference values
- `pytest tests/unit/test_state_store.py` — load/save round-trips for `WheelState`; verifies state file is namespaced as `wheel_state.json`
- `pytest tests/unit/test_logger.py` — verifies `src/logger.py` configures structlog without error; verifies that running a fixture signal scan produces at least one JSON-lines entry in `logs/signals.log` containing `filter_name`, `passed`, and `reason` keys; verifies the signal engine log entry contains `strategy_name`, `ticker`, `total_filters_run`, `final_signal_type`, and `confidence_tier`
- `pytest tests/integration/ -m alpaca` — fetches a real AAPL options chain and asserts Greeks are non-null; verifies full pipeline (`AlpacaPaperAdapter` → `indicator_engine` → `WheelStrategy.evaluate()`) produces a valid signal card with a populated `filter_trace`

**Handoff artifact:** `python run_signals.py --tickers AAPL MSFT --strategy wheel` fetches live data from Alpaca paper API, produces valid JSON signal cards to stdout, and appends to `signals.jsonl`.

---

### Phase 2 — Live Data Adapter (Tradier Sandbox)

**What it builds:**
- `TradierAdapter` implementing `DataAdapter` protocol
- Tradier sandbox API integration (15-min delayed, free tier)
- `alpaca_adapter.py` stub (Alpaca MCP v2) ready for Phase 3 swap-in
- Real ticker watchlist in `config/watchlist.toml`
- APScheduler job: run signal scan at 09:45 ET on trading days
- Rate limiting + retry logic in adapter layer

**Test criterion:** `python run_signals.py --strategy wheel` runs against 5 real tickers (AAPL, MSFT, NVDA, JPM, AMZN) during market hours and returns ≥1 valid signal card with populated Greeks. JSONL log persists across runs. APScheduler fires correctly on next weekday.

**Handoff artifact:** `signals.jsonl` with 5+ real signal cards from Tradier data; APScheduler running in background.

---

### Phase 3 — Alpaca MCP Integration + Claude Reasoning Layer

**What it builds:**
- `AlpacaMCPAdapter` using Alpaca MCP v2 server (replaces Tradier as primary)
- MCP server entry in `~/.claude/mcp.json`
- Claude Code tool wrappers for `get_options_chain`, `get_quote`, `get_account`
- `thesis_generator.py`: calls Claude API (`claude-haiku-4-5`) to generate `thesis_text` from indicator readings
- Phase transition CLI: `python manage_state.py assign --ticker AAPL --strike 175 --shares 100 --cost-basis 174.23`

**Test criterion:** Claude Code session calls `alpaca_get_options_chain("AAPL")` as a tool; result flows through `signal_engine.py` → `WheelStrategy.evaluate()` to produce a complete signal card with AI-generated thesis. `wheel_state.json` updates correctly on assignment command.

**Handoff artifact:** Custom Claude Code slash command `/wheel-scan` (`.claude/commands/`) runs full signal pipeline interactively.

---

### Phase 4 — (Future) Execution Layer

**What it builds:**
- Order placement via Alpaca MCP: sell-to-open puts/calls from signal card
- Assignment monitoring via Alpaca account events
- Auto-update `wheel_state.json` on assignment detection
- Risk guards: max open positions, max notional per ticker, portfolio-level delta cap

**Test criterion:** Paper trading account shows correct option orders placed matching signal card recommendations. Phase transitions fire automatically.

**Handoff artifact:** End-to-end paper trading loop running for 1 full week without manual intervention.

---

## 8. Open Questions & Decisions Required Before Implementation

### Q1 (RESOLVED ✅): Phase 1 data source

**Decision**: **Alpaca paper trading API** — resolved May 2026.

The user has an active Alpaca Markets account with paper trading enabled. `AlpacaPaperAdapter` is built in Phase 1 using `alpaca-py`, routing all requests to `https://paper-api.alpaca.markets`. This provides full real-time options chains with Greeks (delta/gamma/theta/vega/rho) and IV — superior to both Tradier's 15-min delayed sandbox and yfinance's derived-Greeks approach. Tradier and yfinance are retained as documented fallbacks in the adapter layer but are not the default.

Credentials are stored in `.env` (see Section 6 — Alpaca API Credentials). A `FixtureAdapter` is kept for offline unit testing.

---

### Q2 (non-blocking): Watchlist — static curated list vs dynamic screener?

**Recommended default**: Static curated `config/watchlist.toml` (~20–50 hand-picked large-cap tickers) for Phase 1–2. Dynamic screener deferred to Phase 4.

---

### Q3 (non-blocking): Signal persistence — JSONL flat file vs SQLite?

**Recommended default**: JSONL (`signals.jsonl`) for Phase 1–2. SQLite migration in Phase 3 when cross-ticker/date queries become necessary.

---

## 9. Adding a New Strategy in Future

Adding a strategy to claude-trader is a 4-step process. `signal_engine.py`, `state_store.py`, `signal_output.py`, `indicator_engine.py`, and all adapter code remain **completely unchanged**.

**Step 1 — Create `config/{name}.toml`**

Define all filter parameters for the new strategy. This file is the single source of truth for tunable values — nothing is hardcoded. `WheelStrategy` serves as the reference template for required keys.

**Step 2 — Create `src/strategies/{name}/strategy.py`**

Implement the `Strategy` Protocol, including `load_config("config/{name}.toml")`. Optionally add `filters.py` (discrete filter classes returning `FilterResult`) and `state.py` (Pydantic state model) alongside it, following the same pattern as `src/strategies/wheel/`.

```python
# src/strategies/momentum/strategy.py
from src.strategies.base import Strategy, Filter, FilterResult
from src.strategies.momentum.state import MomentumState

class MomentumStrategy:
    name = "momentum"

    def get_filters(self) -> list[Filter]:
        return [...]           # momentum-specific filter chain

    def evaluate(self, market_data: dict) -> Signal:
        ...                    # runs filters in order; assembles and returns Signal

    def get_state_schema(self) -> type[BaseModel]:
        return MomentumState   # momentum_state.json is created automatically
```

**Step 3 — Register in `src/strategies/registry.py`**

```python
from src.strategies.momentum.strategy import MomentumStrategy

STRATEGY_REGISTRY: dict[str, type] = {
    "wheel":    WheelStrategy,
    "momentum": MomentumStrategy,   # ← one line added
}
```

**Step 4 — Run**

```
python run_signals.py --tickers AAPL MSFT --strategy momentum
```

State is automatically isolated to `momentum_state.json`. The Wheel strategy's `wheel_state.json` is unaffected. No other files need touching.

---

*PLAN COMPLETE — 9 sections; broker: Alpaca (official MCP v2, paper trading from Phase 1) with Tradier fallback; stack: Python 3.11 + alpaca-py + pandas-ta + pydantic v2 + APScheduler + rich + structlog; strategy layer: pluggable Strategy Protocol with WheelStrategy as first implementation; filter layer: typed FilterResult Pydantic model with two-pass hard-stop + soft-adjustment evaluation; config layer: per-strategy TOML loaded at startup; logging layer: structlog JSON → logs/signals.log + ConsoleRenderer. Last updated: May 2026.*
