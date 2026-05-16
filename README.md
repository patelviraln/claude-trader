# claude-trader

A personal, signal-only AI trading system implementing the **Wheel options strategy** on US large-cap equities. Phase 1 produces structured trade signal cards from fixture data — no live execution, no broker account required.

---

## What it does

For each ticker you give it, the system runs a 7-step filter chain and emits a structured signal card:

- **SELL_PUT** — sell a cash-secured put (Phase A: no shares held)
- **SELL_CALL** — sell a covered call (Phase B: 100 shares held)
- **NO_SIGNAL** — one or more hard-stop filters failed; reason included

Each signal card includes the recommended strike, expiry, delta estimate, all indicator readings, a confidence tier (HIGH / MEDIUM / LOW), and any risk flags.

---

## Requirements

- Python 3.11+
- Dependencies listed in `pyproject.toml`

Install dependencies:

```bash
pip install -e ".[dev]"
```

Or install the runtime deps directly:

```bash
pip install ta pydantic structlog rich python-dotenv requests APScheduler jinja2 alpaca-py
```

> **Note:** `pandas-ta` is listed in the architecture spec but is not available for Python 3.11 on PyPI. The `ta` library is used instead and provides identical EMA / RSI / Bollinger Band functionality.

---

## Setup

### 1. Copy the env file

```bash
cp .env.example .env
```

Fill in your API keys. For Phase 1 (fixture-only mode) **no keys are needed** — all fields can be left as placeholders.

### 2. Check the wheel config

`config/wheel.toml` contains all 11 strategy parameters:

```toml
[wheel]
ema_period          = 50
rsi_min             = 35
rsi_max             = 65
delta_target        = 0.30
delta_tolerance     = 0.05
dte_min             = 30
dte_max             = 45
bb_period           = 20
bb_std_dev          = 2.0
volume_lookback_bars = 20
soft_flag_confidence_penalty = "HIGH_TO_MEDIUM"
```

### 3. Check the state file

`wheel_state.json` tracks which phase each ticker is in. Edit it manually in Phase 1:

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

- **Phase A** — you hold no shares; system looks for a put to sell
- **Phase B** — you hold 100 shares from put assignment; system looks for a covered call to sell

---

## Running signals

### Basic usage (fixture data, no API keys needed)

```bash
python run_signals.py --tickers WXYZ --adapter fixture
```

### Scan multiple tickers

```bash
python run_signals.py --tickers AAPL MSFT NVDA --adapter fixture
```

### Override phase for all tickers

```bash
python run_signals.py --tickers AAPL --adapter fixture --phase A
python run_signals.py --tickers AAPL --adapter fixture --phase B
```

### Output raw JSON to stdout (no rich display)

```bash
python run_signals.py --tickers WXYZ --adapter fixture --json-only
```

### Use live Alpaca paper trading data (Phase 2+)

```bash
python run_signals.py --tickers AAPL MSFT --adapter alpaca
```

Requires `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and `ALPACA_BASE_URL` in your `.env`.

### Full options

```
--tickers       One or more ticker symbols (required)
--adapter       fixture | alpaca  (default: fixture)
--phase         A | B  — override phase for all tickers
--config        Path to wheel config TOML  (default: config/wheel.toml)
--state         Path to wheel state JSON   (default: wheel_state.json)
--signals-out   Path to JSONL output file  (default: signals.jsonl)
--log-level     DEBUG | INFO | WARNING | ERROR
--json-only     Print raw JSON to stdout; suppress rich terminal display
```

---

## Understanding the output

### Terminal display (rich)

```
╭─────────────────────────────── WXYZ  SELL_PUT  HIGH ────────────────╮
│  Strike          $170.00                                             │
│  Expiry          2026-06-22  (DTE 37)                               │
│  Delta           -0.311                                              │
│  IV Rank         24.9%                                              │
│  Underlying      $188.82                                             │
│  EMA50           186.99  (slope +0.0053)                            │
│  RSI14           51.4                                               │
│  BB %B           0.58                                               │
│  HVN             $187.00  (support)                                 │
│  Rationale       All 6 filters passed cleanly                       │
╰──────────────────────────────────────────────────────────────────────╯
```

Color coding: **green** = HIGH confidence, **yellow** = MEDIUM, **red** = LOW.

### JSON signal card schema

```json
{
  "ticker": "WXYZ",
  "phase": "SELL_PUT",
  "signal_timestamp": "2026-05-16T12:00:00Z",
  "underlying_price": 188.82,
  "recommended_strike": 170.0,
  "recommended_expiry": "2026-06-22",
  "dte": 37,
  "delta_estimate": -0.311,
  "iv_rank": 24.9,
  "indicator_readings": {
    "ema50": 186.99,
    "ema50_slope": 0.0053,
    "rsi14": 51.4,
    "bb_upper": 194.77,
    "bb_lower": 180.77,
    "bb_percent_b": 0.58,
    "volume_node_nearest": 187.0,
    "volume_node_type": "support"
  },
  "thesis_text": "",
  "confidence_tier": "HIGH",
  "confidence_rationale": "All 6 filters passed cleanly",
  "risk_flags": [],
  "no_signal_reason": null
}
```

### Confidence tiers

| Tier | Meaning |
|---|---|
| **HIGH** | All 6 filters passed cleanly; BB favorable; volume node confirms strike; delta within ±0.02 of target |
| **MEDIUM** | Core filters pass; 1 soft flag (BB unfavorable or volume divergence); delta within ±0.05 |
| **LOW** | Core filters pass; 2+ soft flags; or delta at edge of tolerance; or IV rank missing |

### Hard stops vs soft flags

| Filter | Type | Effect |
|---|---|---|
| EMA trend (price > EMA50 AND slope > 0) | **Hard stop** | Signal blocked entirely |
| RSI in range 35–65 | **Hard stop** | Signal blocked entirely |
| BB position favorable | **Soft flag** | Confidence lowered; signal still emitted |
| Volume node anchor | **Soft flag** | Risk flag added; signal still emitted |

---

## Filter pipeline

For each ticker the system runs these 7 filters cheapest-first:

```
1. Phase Gate      — validate phase A or B in wheel_state.json
2. EMA Trend       — price > EMA50 AND 5-bar slope > 0           [HARD STOP]
3. RSI Gate        — 35 ≤ RSI(14) ≤ 65                           [HARD STOP]
4. Bollinger Band  — price positioning check                       [soft flag]
5. Options Chain   — fetch chain; check data exists               [HARD STOP]
6. Delta Target    — find strike nearest ±0.30 delta (±0.05 tol) [HARD STOP]
7. Volume Profile  — strike anchored to high-volume node          [soft flag]
```

---

## Signal persistence

Every signal card (including NO_SIGNAL) is appended as one JSON line to `signals.jsonl`:

```bash
# View today's signals
cat signals.jsonl | python -m json.tool

# Count signals by type
grep -o '"phase":"[^"]*"' signals.jsonl | sort | uniq -c
```

Structured logs (structlog JSON) are written to `logs/signals.log`.

---

## Managing phase state

Edit `wheel_state.json` manually when a put is assigned or a covered call is exercised:

**Put assigned → move to Phase B:**
```json
"AAPL": {
  "phase": "B",
  "shares_held": 100,
  "cost_basis": 172.50,
  "assignment_date": "2026-05-17",
  "open_put_strike": 175.0,
  "open_put_expiry": "2026-05-16"
}
```

**Covered call exercised → back to Phase A:**
```json
"AAPL": {
  "phase": "A",
  "shares_held": 0,
  "cost_basis": null,
  "assignment_date": null,
  "open_put_strike": null,
  "open_put_expiry": null
}
```

---

## Adding your own tickers

1. Add an entry to `wheel_state.json` for the ticker
2. If using fixture adapter: add `{TICKER}_ohlcv.json` and `{TICKER}_options.json` to `fixtures/` (see existing files for format)
3. If using alpaca adapter: no fixture files needed — data is fetched live

---

## Running tests

```bash
pytest tests/
```

Expected output:

```
48 passed in 0.8s
```

Test coverage:
- `tests/unit/test_indicator_engine.py` — EMA, RSI, Bollinger, volume profile
- `tests/unit/test_filters.py` — all 7 filter classes
- `tests/unit/test_state_store.py` — JSON state read/write helpers
- `tests/integration/test_fixture_scenarios.py` — end-to-end: SELL_PUT, SELL_CALL, EMA fail, RSI fail, soft flags

---

## Project structure

```
claude-trader/
├── run_signals.py              # CLI entry point
├── pyproject.toml              # dependencies and pytest config
├── wheel_state.json            # per-ticker phase state (edit manually)
├── signals.jsonl               # JSONL signal log (appended each run)
├── config/
│   └── wheel.toml              # all 11 strategy parameters
├── fixtures/
│   ├── WXYZ_ohlcv.json         # example: clear SELL_PUT scenario
│   ├── WXYZ_options.json
│   ├── CALLX_ohlcv.json        # example: clear SELL_CALL scenario
│   ├── CALLX_options.json
│   ├── EMAFAIL_ohlcv.json      # example: EMA hard stop
│   ├── RSIFAIL_ohlcv.json      # example: RSI hard stop
│   ├── LOWCONF_ohlcv.json      # example: 2 soft flags → LOW confidence
│   └── regen.py                # regenerate fixture data
├── src/
│   ├── indicator_engine.py     # EMA, RSI, Bollinger, volume profile
│   ├── signal_engine.py        # orchestrator: adapter → strategy → output
│   ├── signal_output.py        # SignalCard schema + rich formatter + JSONL logger
│   ├── logger.py               # structlog dual-renderer setup
│   ├── state_store.py          # read/write wheel_state.json
│   ├── adapters/
│   │   ├── base.py             # DataAdapter Protocol
│   │   ├── fixture_adapter.py  # loads from fixtures/
│   │   └── alpaca_adapter.py   # live Alpaca paper trading (Phase 2+)
│   └── strategies/
│       ├── base.py             # Strategy Protocol + FilterResult model
│       ├── registry.py         # strategy name → class registry
│       └── wheel/
│           ├── state.py        # WheelState Pydantic model
│           ├── filters.py      # 7 filter classes
│           └── strategy.py     # WheelStrategy (two-pass evaluation)
├── tests/
│   ├── unit/
│   │   ├── test_indicator_engine.py
│   │   ├── test_filters.py
│   │   └── test_state_store.py
│   └── integration/
│       └── test_fixture_scenarios.py
└── logs/
    └── signals.log             # structured JSON log (created on first run)
```

---

## Roadmap

| Phase | Status | What it adds |
|---|---|---|
| **1 — Static pipeline** | ✅ Complete | Fixture data, all 7 filters, signal cards, CLI |
| **2 — Live data** | Planned | Tradier sandbox adapter, real options chains with Greeks, APScheduler daily trigger |
| **3 — Alpaca MCP + Claude reasoning** | Planned | Alpaca MCP v2 integration, AI-generated `thesis_text` via Claude API, phase transition CLI |
| **4 — Execution layer** | Future | Paper order placement, auto phase transitions, risk guards |

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ALPACA_API_KEY` | Phase 2+ | Alpaca API key |
| `ALPACA_SECRET_KEY` | Phase 2+ | Alpaca secret key |
| `ALPACA_BASE_URL` | Phase 2+ | `https://paper-api.alpaca.markets` for paper trading |
| `TRADIER_API_TOKEN` | Phase 2 fallback | Tradier sandbox token |
| `POLYGON_API_KEY` | Optional | Polygon.io for historical data |
| `ANTHROPIC_API_KEY` | Phase 3+ | For AI-generated thesis text |
