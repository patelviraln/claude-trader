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

**Phase 1 note**: Tradier sandbox (free, no credit card) is recommended for Phase 1 to avoid coupling early development to account state — Alpaca becomes primary in Phase 2+.

---

## 2. System Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        WHEEL SIGNAL SYSTEM                      │
│                                                                 │
│  ┌─────────────┐    ┌──────────────────┐    ┌───────────────┐  │
│  │  DATA       │    │  INDICATOR       │    │  SIGNAL       │  │
│  │  INGESTION  │───▶│  COMPUTATION     │───▶│  ENGINE       │  │
│  │             │    │                  │    │               │  │
│  │ • OHLCV bars│    │ • 50-EMA + slope │    │ • Filter chain│  │
│  │ • Options   │    │ • RSI(14)        │    │ • Phase check │  │
│  │   chains    │    │ • BB(20,2)       │    │ • Decision    │  │
│  │ • Volume    │    │ • Volume profile │    │   tree        │  │
│  │   history   │    │ • Delta lookup   │    │ • Confidence  │  │
│  └─────────────┘    └──────────────────┘    └───────┬───────┘  │
│       ▲                                             │           │
│       │                                             ▼           │
│  ┌────┴──────┐                            ┌─────────────────┐  │
│  │  MCP /    │                            │  SIGNAL OUTPUT  │  │
│  │  REST     │                            │  LAYER          │  │
│  │  ADAPTER  │                            │                 │  │
│  │           │                            │ • JSON cards    │  │
│  │  Alpaca   │                            │ • Rich terminal │  │
│  │  MCP v2   │                            │ • JSONL log     │  │
│  │  (or      │                            └─────────────────┘  │
│  │  Tradier  │                                                  │
│  │  sandbox) │     ┌──────────────────┐                        │
│  └───────────┘     │  STATE STORE     │                        │
│                    │  (phase_state.   │                        │
│                    │   json)          │                        │
│                    │                  │                        │
│                    │ per-ticker:      │                        │
│                    │ • phase (A/B)    │                        │
│                    │ • cost_basis     │                        │
│                    │ • shares_held    │                        │
│                    └──────────────────┘                        │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  FUTURE: EXECUTION LAYER (Phase 3+)                     │   │
│  │  Alpaca MCP → order placement via Claude tool calls     │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Component Specifications

| Component | Inputs | Outputs | Technology | Why |
|---|---|---|---|---|
| **MCP/REST Adapter** | Ticker list, date | OHLCV bars, options chains, Greeks | `alpaca-py` SDK or `requests` (Tradier) | Abstracts broker; swappable via config |
| **Data Ingestion** | Raw API responses | Normalized DataFrames (OHLCV, options chain) | `pandas`, `alpaca-py` | Standard DataFrame pipeline |
| **Indicator Computation** | OHLCV DataFrame | EMA series, RSI, BB bands, volume nodes | `pandas-ta` | Pure-Python, no C deps; covers all 4 indicators |
| **Signal Engine** | Indicator values, options chain, phase state | Raw signal + confidence | Pure Python logic (`signal_engine.py`) | All conditional logic in one testable module |
| **State Store** | Assignment events | Current phase per ticker | `phase_state.json` (local file, read/write) | Simple, portable; no DB overhead for Phase 1 |
| **Signal Output Layer** | Raw signal | Formatted signal card | `pydantic` (schema) + `rich` (terminal) | Typed output; pretty-prints to terminal and JSONL |
| **Scheduler** | Config (tickers, times) | Triggers pipeline | `APScheduler` | Lightweight; runs in-process; no Redis needed |

### Claude Code vs Script vs MCP Breakdown

- **Claude Code tasks**: Interpreting signal cards, generating thesis_text, resolving edge cases, running ad-hoc signal checks interactively
- **Standalone scripts**: `run_signals.py` (daily batch), `backfill_indicators.py` (historical data)
- **MCP tool calls**: `alpaca-mcp` for options chain fetch and (Phase 3) order placement

---

## 3. Signal Pipeline Design

### Filter Execution Order (cheapest-first)

```
For each ticker in universe:

  STEP 1: Phase Gate [O(1), no I/O]
  ├── Read phase_state.json for this ticker
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
  └── Assemble signal card (see Section 4)
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

**State tracked per ticker in `phase_state.json`:**
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

**Phase A → Phase B trigger**: User manually updates `phase_state.json` when put is assigned (Phase 1 is signal-only; no execution API). Phase 3 will auto-update via assignment event webhook.

**Phase B → Phase A trigger**: User manually updates when covered call is exercised (shares called away).

**Conflict Resolution Rules (strict precedence):**

1. **50-EMA declining → HARD STOP.** All other indicators are overridden. RSI in range and BB at lower band do not override a declining EMA. Rationale: trending down means the mean-reversion put-sell thesis is invalid.
2. **RSI out of range (>65 or <35) → HARD STOP.** Overbought/oversold conditions signal extremes not appropriate for wheel entry.
3. **BB position unfavorable → SOFT FLAG only.** Lowers confidence tier from HIGH → MEDIUM. Does not block signal.
4. **Volume node divergence >5% from delta target → SOFT FLAG only.** Noted in risk_flags; does not block signal.

---

## 4. Signal Output Schema

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
  "no_signal_reason": "string | null (populated only when phase = NO_SIGNAL)"
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
  "no_signal_reason": null
}
```

---

## 5. Technical Stack Specification

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
- Loaded via `python-dotenv` at startup
- No secrets in source code or signal output files

```
# .env
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TRADIER_API_TOKEN=...
POLYGON_API_KEY=...   # optional, Phase 1 only
```

---

## 6. Implementation Phases

### Phase 1 — Static Pipeline (No Broker Account Required)

**What it builds:**
- Project scaffold: `pyproject.toml`, `src/` layout, `.env.example`, `.gitignore`
- `DataAdapter` interface + `FixtureAdapter` (loads from `fixtures/` JSON files)
- `indicator_engine.py`: computes EMA50, RSI14, BB(20,2), volume profile from OHLCV DataFrame
- `signal_engine.py`: full filter chain + decision tree + confidence tier logic
- `phase_state.json`: manual state store with read/write helpers
- `signal_output.py`: Pydantic schema + `rich` terminal formatter + JSONL logger
- `run_signals.py`: CLI entry point (`--tickers AAPL MSFT --phase A`)

**Fixture data:** 3–5 pre-saved OHLCV + options chain JSON files in `fixtures/` representing known scenarios: clear PUT signal, clear CALL signal, EMA fail, RSI fail, LOW confidence (soft flags).

**Test criterion:** `pytest tests/` passes 100% on all fixture scenarios; signal cards match expected output exactly; all 4 indicator values verified against manually computed reference values.

**Handoff artifact:** `python run_signals.py --tickers WXYZ --phase A` produces a valid JSON signal card to stdout and appends to `signals.jsonl`.

---

### Phase 2 — Live Data Adapter (Tradier Sandbox)

**What it builds:**
- `TradierAdapter` implementing `DataAdapter` protocol
- Tradier sandbox API integration (15-min delayed, free tier)
- `alpaca_adapter.py` stub (Alpaca MCP v2) ready for Phase 3 swap-in
- Real ticker watchlist in `config/watchlist.toml`
- APScheduler job: run signal scan at 09:45 ET on trading days
- Rate limiting + retry logic in adapter layer

**Test criterion:** `python run_signals.py` runs against 5 real tickers (AAPL, MSFT, NVDA, JPM, AMZN) during market hours and returns ≥1 valid signal card with populated Greeks. JSONL log persists across runs. APScheduler fires correctly on next weekday.

**Handoff artifact:** `signals.jsonl` with 5+ real signal cards from Tradier data; APScheduler running in background.

---

### Phase 3 — Alpaca MCP Integration + Claude Reasoning Layer

**What it builds:**
- `AlpacaMCPAdapter` using Alpaca MCP v2 server (replaces Tradier as primary)
- MCP server entry in `~/.claude/mcp.json`
- Claude Code tool wrappers for `get_options_chain`, `get_quote`, `get_account`
- `thesis_generator.py`: calls Claude API (`claude-haiku-4-5`) to generate `thesis_text` from indicator readings
- Phase transition CLI: `python manage_state.py assign --ticker AAPL --strike 175 --shares 100 --cost-basis 174.23`

**Test criterion:** Claude Code session calls `alpaca_get_options_chain("AAPL")` as a tool; result flows through signal_engine to produce a complete signal card with AI-generated thesis. Phase state file updates correctly on assignment command.

**Handoff artifact:** Custom Claude Code slash command `/wheel-scan` (`.claude/commands/`) runs full signal pipeline interactively.

---

### Phase 4 — (Future) Execution Layer

**What it builds:**
- Order placement via Alpaca MCP: sell-to-open puts/calls from signal card
- Assignment monitoring via Alpaca account events
- Auto-update `phase_state.json` on assignment detection
- Risk guards: max open positions, max notional per ticker, portfolio-level delta cap

**Test criterion:** Paper trading account shows correct option orders placed matching signal card recommendations. Phase transitions fire automatically.

**Handoff artifact:** End-to-end paper trading loop running for 1 full week without manual intervention.

---

## 7. Open Questions & Decisions Required Before Implementation

### Q1 (BLOCKING): Phase 1 data source — Tradier sandbox vs yfinance + py_vollib?

**Option A — Tradier sandbox** (recommended): Free signup (~2 min); full options chains with Greeks and IV; 15-min delayed. Phase 1 data format matches Phase 2 live data exactly — no adapter rewrite needed.

**Option B — yfinance + py_vollib** (zero-signup fallback): No account needed. `yfinance` does not return Greeks; must calculate delta/theta locally from price + IV using `py_vollib`. Adds one extra module but removes all signup friction.

**Decision needed**: Can you create a free Tradier sandbox account? If yes → Tradier. If no → yfinance + py_vollib.

---

### Q2 (non-blocking): Watchlist — static curated list vs dynamic screener?

**Recommended default**: Static curated `config/watchlist.toml` (~20–50 hand-picked large-cap tickers) for Phase 1–2. Dynamic screener deferred to Phase 4.

---

### Q3 (non-blocking): Signal persistence — JSONL flat file vs SQLite?

**Recommended default**: JSONL (`signals.jsonl`) for Phase 1–2. SQLite migration in Phase 3 when cross-ticker/date queries become necessary.

---

*PLAN COMPLETE — 7 sections; broker: Alpaca (official MCP v2) with Tradier fallback; stack: Python 3.11 + pandas-ta + pydantic v2 + APScheduler + rich.*
