# claude-trader

A personal AI-assisted paper trading system implementing the **Wheel options strategy** on US large-cap equities. Generates structured signal cards, places paper orders via Alpaca, and serves a live dashboard — all driven by a 7-filter pipeline and an optional Claude API thesis layer.

---

## What it does

For each ticker the system runs a 7-step filter chain and emits a structured signal card:

- **SELL_PUT** — sell a cash-secured put (Phase A: no shares held)
- **SELL_CALL** — sell a covered call (Phase B: 100 shares held)
- **NO_SIGNAL** — a hard-stop filter failed; reason included

Each card includes the recommended strike, expiry, delta estimate, IV rank, option mid price, indicator readings, a confidence tier (HIGH / MEDIUM / LOW), risk flags, an optional AI-generated thesis, and — when `--execute` is used — the Alpaca order ID.

---

## Requirements

- Python 3.11+
- An [Alpaca](https://alpaca.markets) paper trading account (free)
- Optional: `ANTHROPIC_API_KEY` for AI thesis generation

```bash
pip install -e ".[dev]"
```

Or directly:

```bash
pip install ta pydantic structlog rich python-dotenv requests APScheduler jinja2 \
            alpaca-py fastapi uvicorn python-multipart anthropic
```

---

## Setup

### 1. Copy and fill the env file

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `ALPACA_API_KEY` | Yes (live/execute) | Alpaca paper API key |
| `ALPACA_SECRET_KEY` | Yes (live/execute) | Alpaca paper secret key |
| `ALPACA_BASE_URL` | Yes (live/execute) | `https://paper-api.alpaca.markets` |
| `ANTHROPIC_API_KEY` | Optional | Enables AI thesis via `--thesis` flag |

For fixture-only mode (`--adapter fixture`) **no keys are needed**.

### 2. Configure strategy parameters

`config/wheel.toml` contains all strategy and scheduler settings:

```toml
[wheel]
ema_period           = 50
rsi_min              = 35
rsi_max              = 65
delta_target         = 0.30
delta_tolerance      = 0.05
dte_min              = 30
dte_max              = 45
bb_period            = 20
bb_std_dev           = 2.0
volume_lookback_bars = 20

[scheduler]
tickers   = ["NVDA", "AAPL", "MSFT"]
adapter   = "alpaca"
run_hour  = 9
run_minute = 35
timezone  = "America/New_York"
```

### 3. Initialise ticker state

`wheel_state.json` tracks which phase each ticker is in. Add entries manually or via `phase_transition.py`:

```json
{
  "NVDA": {
    "phase": "A",
    "shares_held": 0,
    "cost_basis": null,
    "assignment_date": null,
    "open_put_strike": null,
    "open_put_expiry": null
  }
}
```

---

## Running signals

### Fixture data (no API keys needed)

```bash
python run_signals.py --tickers WXYZ --adapter fixture
python run_signals.py --tickers WXYZ CALLX --adapter fixture --json-only
```

### Live Alpaca data

```bash
python run_signals.py --tickers AAPL MSFT NVDA --adapter alpaca
```

### Place paper orders when a signal fires

```bash
python run_signals.py --tickers MSFT --adapter alpaca --execute
```

Submits a day-limit sell-to-open order at the option mid price. Order ID and status appear in the signal card.

### Generate AI thesis (requires `ANTHROPIC_API_KEY`)

```bash
python run_signals.py --tickers NVDA --adapter alpaca --thesis
```

Calls `claude-opus-4-7` with adaptive thinking and a cached system prompt. Returns "" silently if no API key is set.

### All flags

```
--tickers       One or more ticker symbols (required)
--adapter       fixture | alpaca  (default: fixture)
--phase         A | B  — override phase for all tickers this run
--config        Path to wheel.toml  (default: config/wheel.toml)
--state         Path to wheel_state.json  (default: wheel_state.json)
--signals-out   Path to JSONL output  (default: signals.jsonl)
--log-level     DEBUG | INFO | WARNING | ERROR
--json-only     Print raw JSON; suppress rich terminal display
--thesis        Generate AI thesis via Claude API
--execute       Place Alpaca paper orders for actionable signals
```

---

## Dashboard

```bash
streamlit run streamlit_app.py
# opens http://localhost:8501
```

The dashboard has five pages (sidebar navigation):

| Page | What it shows |
|---|---|
| **Dashboard** | Ticker cards — phase, last signal, price, IV rank, option mid, confidence, order ID |
| **Ticker Detail** | IV history line chart (Plotly), full signal history table, phase transition form |
| **Run Signals** | Select tickers + adapter + `--execute` / `--thesis` flags, run scan directly from UI |
| **Backtest** | Walk-forward replay on historical OHLCV: equity curve, trade log, win rate, premium collected |
| **Configuration** | Strategy parameters, scheduler settings, add/remove tracked tickers |

---

## Automated daily scheduler

Runs the full signal scan every trading day at the configured time (default 9:35 AM ET):

```bash
python scheduler.py                          # starts blocking cron loop
python scheduler.py --run-now                # fire once immediately and exit
python scheduler.py --config path/to.toml   # custom config
```

---

## Phase transitions

Record a put assignment (move Phase A → B):

```bash
python phase_transition.py assign NVDA --cost-basis 880.50
```

Record an exit (call exercised or expired, move Phase B → A):

```bash
python phase_transition.py exit NVDA
```

Or use the **Assign / Exit** buttons in the dashboard.

---

## Understanding signal output

### Terminal display

```
╭────────────────────── MSFT  SELL_PUT  MEDIUM ──────────────────────╮
│  Strike          $405.00                                            │
│  Expiry          2026-06-18  (DTE 33)                               │
│  Delta           -0.314                                             │
│  IV Rank         100.0%                                             │
│  Underlying      $422.00                                            │
│  EMA50           404.51  (slope +0.2949)                            │
│  RSI14           58.3                                               │
│  BB %B           0.65                                               │
│  HVN             $406.50  (support)                                 │
│  Option Mid      $7.86                                              │
│  Rationale       Soft flag: bb_unfavorable                          │
│  Risk flags      BB position unfavorable for entry                  │
│  Order ID        a0c8dc2b-…  [OrderStatus.ACCEPTED]                 │
╰─────────────────────────────────────────────────────────────────────╯
```

### Signal card schema

```json
{
  "ticker": "MSFT",
  "phase": "SELL_PUT",
  "signal_timestamp": "2026-05-16T17:02:49Z",
  "underlying_price": 422.00,
  "recommended_strike": 405.0,
  "recommended_expiry": "2026-06-18",
  "dte": 33,
  "delta_estimate": -0.314,
  "iv_rank": 100.0,
  "option_mid": 7.86,
  "thesis_text": "",
  "confidence_tier": "MEDIUM",
  "confidence_rationale": "Soft flag: bb_unfavorable",
  "risk_flags": ["BB position unfavorable for entry"],
  "order_id": "a0c8dc2b-8d89-44c6-bcaf-46be6f2691f2",
  "order_status": "accepted",
  "no_signal_reason": null
}
```

### Confidence tiers

| Tier | Meaning |
|---|---|
| **HIGH** | All filters pass cleanly; delta within ±0.02; IV rank available |
| **MEDIUM** | 1 soft flag (BB unfavorable or volume divergence); or delta within ±0.05; or IV rank missing |
| **LOW** | 2+ soft flags; or delta at tolerance edge |

---

## Filter pipeline

```
1. Phase Gate      — validate phase A or B in wheel_state.json        [hard stop]
2. EMA Trend       — price > EMA50 AND 5-bar slope > 0                [hard stop]
3. RSI Gate        — 35 ≤ RSI(14) ≤ 65                                [hard stop]
4. Bollinger Band  — price positioning check                           [soft flag]
5. Options Chain   — fetch chain; verify data exists                   [hard stop]
6. Delta Target    — find strike nearest ±0.30 delta (±0.05 tol)      [hard stop]
7. Volume Profile  — strike anchored to high-volume node              [soft flag]
```

---

## IV rank

The system maintains a rolling IV history in `iv_history/{TICKER}.jsonl`. After each scan the ATM put IV is appended, and IV rank is computed as:

```
IV Rank = (current_IV - 52w_low) / (52w_high - 52w_low) × 100
```

Returns `None` (shown as "unavailable") until at least 2 samples exist.

---

## Tests

```bash
pytest
```

**168 tests passing** across:

| File | What it covers |
|---|---|
| `test_indicator_engine.py` | EMA, RSI, Bollinger, volume profile, IV rank |
| `test_filters.py` | All 7 filter classes |
| `test_state_store.py` | JSON state read/write helpers |
| `test_fixture_scenarios.py` | End-to-end: SELL_PUT, SELL_CALL, EMA fail, RSI fail, soft flags |
| `test_alpaca_adapter.py` | Hardened live adapter (mocked Alpaca SDK) |
| `test_iv_history.py` | append_iv_sample, load_iv_series |
| `test_scheduler.py` | load_scheduler_config, build_adapter, run_job |
| `test_thesis_generator.py` | Claude API thesis (mocked Anthropic client) |
| `test_phase_transition.py` | assign/exit commands, idempotency, multi-ticker |
| `test_order_executor.py` | OCC symbol building, order placement, error guards |
| `test_dashboard_data.py` | Data layer for the dashboard |
| `test_backtester.py` | BacktestEngine walk-forward logic, BS pricing, delta strike search |

---

## Project structure

```
claude-trader/
├── run_signals.py              # CLI: scan tickers, optional --execute / --thesis
├── streamlit_app.py            # Streamlit dashboard (streamlit run streamlit_app.py)
├── scheduler.py                # APScheduler daily cron (python scheduler.py)
├── phase_transition.py         # Phase A↔B CLI (assign / exit)
├── pyproject.toml
├── wheel_state.json            # per-ticker phase state
├── signals.jsonl               # JSONL signal log
├── iv_history/                 # rolling IV samples per ticker
│   └── {TICKER}.jsonl
├── config/
│   └── wheel.toml              # strategy + scheduler parameters
├── fixtures/                   # deterministic test data
│   ├── WXYZ_ohlcv.json / _options.json   (SELL_PUT scenario)
│   ├── CALLX_ohlcv.json / _options.json  (SELL_CALL scenario)
│   ├── EMAFAIL / RSIFAIL / LOWCONF       (failure scenarios)
│   └── regen.py
├── templates/                  # Jinja2 HTML templates
│   ├── base.html               # layout, Chart.js, CSS
│   ├── dashboard.html          # ticker grid + modals
│   └── ticker.html             # IV chart + signal history
├── src/
│   ├── indicator_engine.py     # EMA, RSI, Bollinger, volume profile, IV rank
│   ├── backtester.py           # walk-forward BacktestEngine (BS pricing, realized vol)
│   ├── signal_engine.py        # orchestrator: adapter → strategy → output → order
│   ├── signal_output.py        # SignalCard schema, rich formatter, JSONL logger
│   ├── dashboard_data.py       # data layer for the web UI
│   ├── iv_history.py           # rolling IV persistence (append / load)
│   ├── order_executor.py       # AlpacaOrderExecutor: OCC symbol + limit order
│   ├── thesis_generator.py     # Claude API thesis (opus-4-7, adaptive thinking)
│   ├── logger.py               # structlog dual-renderer
│   ├── state_store.py          # atomic JSON read/write for wheel_state.json
│   ├── adapters/
│   │   ├── base.py             # DataAdapter Protocol
│   │   ├── fixture_adapter.py  # loads from fixtures/
│   │   └── alpaca_adapter.py   # live Alpaca paper trading (IEX feed)
│   └── strategies/
│       ├── base.py             # Strategy Protocol + FilterResult
│       ├── registry.py
│       └── wheel/
│           ├── state.py        # WheelState Pydantic model
│           ├── filters.py      # 7 filter classes
│           └── strategy.py     # WheelStrategy (two-pass evaluation)
└── tests/
    ├── unit/                   # 11 test modules
    └── integration/
        └── test_fixture_scenarios.py
```

---

## Roadmap

| Phase | Status | What it adds |
|---|---|---|
| **1 — Signal pipeline** | ✅ Complete | 7-filter Wheel pipeline, fixture adapter, signal cards, CLI |
| **2 — Live data** | ✅ Complete | Alpaca live adapter (IEX feed, OCC parsing), IV rank/history, APScheduler |
| **3 — AI thesis + transitions** | ✅ Complete | Claude API thesis (opus-4-7, adaptive thinking, tool use), phase_transition.py CLI |
| **4 — Order execution** | ✅ Complete | AlpacaOrderExecutor, OCC symbol builder, `--execute` flag, option mid in cards |
| **5 — Dashboard** | ✅ Complete | Streamlit UI: ticker grid, IV history chart, signal table, phase transition forms, configuration |
| **6 — Backtesting** | ✅ Complete | Walk-forward replay on historical OHLCV; BS-simulated entries; equity curve, win rate, premium collected, assignment rate — integrated as a Backtest page in the dashboard |
