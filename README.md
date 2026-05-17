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
pip install ta pydantic structlog rich python-dotenv requests APScheduler \
            alpaca-py anthropic streamlit plotly tomli-w
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
| **Dashboard** | Ticker cards — phase, last signal, price, IV rank, option mid, confidence, order ID. **View Details** navigates directly to Ticker Detail for that ticker. |
| **Ticker Detail** | IV history line chart (Plotly), full signal history table, phase transition form |
| **Run Signals** | Select adapter (Alpaca default) and tickers, run the 7-filter scan from the UI; fixture adapter filters to tickers with available fixture files |
| **Backtest** | Walk-forward replay on historical OHLCV: equity curve, trade log, win rate, premium collected. Ticker dropdown and date range. |
| **Configuration** | Strategy parameters, scheduler settings, add/remove tracked tickers |

**Adapter behaviour:** Alpaca is the default adapter on all pages when `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set in `.env`. Fixture adapter is always available as a fallback for offline / test use. Switching adapters immediately updates the ticker dropdown without requiring form submission.

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

The schema is strategy-agnostic (Track A refactor). Option-specific fields live in `legs[]`; strategy-specific extras in `payload{}`.

```json
{
  "strategy_name": "wheel",
  "ticker": "MSFT",
  "signal_type": "SELL_PUT",
  "signal_timestamp": "2026-05-16T17:02:49Z",
  "underlying_price": 422.00,
  "legs": [
    {
      "asset_class": "option",
      "symbol": "MSFT",
      "side": "sell",
      "qty": 1,
      "order_type": "limit",
      "limit_price": 7.86,
      "strike": 405.0,
      "expiry": "2026-06-18",
      "option_type": "put",
      "delta_estimate": -0.314
    }
  ],
  "indicators": {
    "ema50": 404.51, "ema50_slope": 0.2949,
    "rsi14": 58.3, "bb_percent_b": 0.65,
    "volume_node_nearest": 406.50, "volume_node_type": "support"
  },
  "payload": { "dte": 33, "iv_rank": 100.0 },
  "thesis_text": "",
  "confidence_tier": "MEDIUM",
  "confidence_rationale": "Soft flag: bb_unfavorable",
  "risk_flags": ["BB position unfavorable for entry"],
  "no_signal_reason": null,
  "order_ids": ["a0c8dc2b-8d89-44c6-bcaf-46be6f2691f2"],
  "order_statuses": ["accepted"]
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
4. Bollinger Band  — %B ≤ 0.20 (Phase A) or %B ≥ 0.80 (Phase B)      [soft flag]
5. Options Chain   — fetch chain; verify data exists                   [hard stop]
6. Delta Target    — find strike nearest ±0.30 delta (±0.05 tol)      [hard stop]
7. Volume Profile  — strike anchored to high-volume node              [soft flag]
```

All 7 filters implement a unified `run(market_data, state, config) → FilterResult` protocol defined in `src/strategies/base.py`.

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

**198 tests passing** across:

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
| `test_backtester.py` | BacktestEngine walk-forward logic, BS pricing, delta strike search, simulate_trade Protocol |
| `test_state_store.py` | Old flat-file API + new per-strategy get/set/all_tickers API |
| `test_router.py` | Strategy resolution, grouped assignments, build_strategy |
| `test_alpaca_adapter.py` | get_ohlcv, get_options_chain, get_multi_ohlcv, fixture quote/option stubs |

---

## Project structure

```
claude-trader/
├── run_signals.py              # CLI: scan tickers, optional --execute / --thesis / --strategy
├── streamlit_app.py            # Streamlit dashboard (streamlit run streamlit_app.py)
├── scheduler.py                # APScheduler daily cron (python scheduler.py)
├── phase_transition.py         # Phase A↔B CLI (assign / exit)
├── pyproject.toml
├── wheel_state.json            # Wheel per-ticker state (legacy; migrates to state/wheel.json)
├── signals.jsonl               # JSONL signal log (strategy-agnostic schema)
├── iv_history/                 # rolling IV samples per ticker
│   └── {TICKER}.jsonl
├── state/                      # per-strategy state files (Phase A3)
│   └── {strategy}.json
├── config/
│   ├── wheel.toml              # Wheel strategy parameters
│   └── strategies.toml         # strategy router: ticker → strategy mapping
├── scripts/                    # one-shot migration helpers
│   ├── migrate_signals_jsonl.py
│   └── migrate_wheel_state.py
├── fixtures/                   # deterministic test data
│   ├── WXYZ_ohlcv.json / _options.json   (SELL_PUT scenario)
│   ├── CALLX_ohlcv.json / _options.json  (SELL_CALL scenario)
│   ├── EMAFAIL / RSIFAIL / LOWCONF       (failure scenarios)
│   └── regen.py
├── src/
│   ├── indicator_engine.py     # EMA, RSI, Bollinger, volume profile, IV rank
│   ├── backtester.py           # walk-forward BacktestEngine (delegates P&L to strategy)
│   ├── signal_engine.py        # thin orchestrator: adapter → strategy.emit_signal_card → output
│   ├── signal_output.py        # Leg + SignalCard schema, rich formatter, JSONL logger
│   ├── dashboard_data.py       # data layer for the web UI
│   ├── iv_history.py           # rolling IV persistence (append / load)
│   ├── order_executor.py       # OrderIntent + execute() — single/multi-leg Alpaca orders
│   ├── thesis_generator.py     # Claude API thesis (opus-4-7, adaptive thinking)
│   ├── router.py               # strategy router: ticker → strategy instance via strategies.toml
│   ├── logger.py               # structlog dual-renderer
│   ├── state_store.py          # flat-file + per-strategy state API
│   ├── adapters/
│   │   ├── base.py             # DataAdapter + OptionsDataAdapter Protocols
│   │   ├── fixture_adapter.py  # loads from fixtures/ (stubs new A4 methods)
│   │   └── alpaca_adapter.py   # live Alpaca (IEX feed, get_quote, get_multi_ohlcv)
│   └── strategies/
│       ├── base.py             # Strategy/Filter/TradeResult Protocols + FilterResult
│       ├── registry.py         # @register decorator + get() / list_strategies()
│       ├── spreads/            # Track B — defined-risk option spreads (in progress)
│       └── wheel/
│           ├── state.py        # WheelState Pydantic model
│           ├── filters.py      # 7 filter classes
│           ├── backtester.py   # BS pricing helpers + simulate_wheel_trade()
│           └── strategy.py     # WheelStrategy: emit_signal_card + simulate_trade
└── tests/
    ├── unit/                   # 12 test modules
    └── integration/
        └── test_fixture_scenarios.py
```

---

## Code quality

| Area | Detail |
|---|---|
| **Strategy Protocol** | `Strategy.emit_signal_card(ticker, adapter, state, context?) → SignalCard` — orchestrator knows nothing about Wheel; each strategy owns its full signal assembly |
| **Multi-strategy schema** | `SignalCard` uses `signal_type: str`, `legs: list[Leg]`, `indicators: dict`, `payload: dict` — strategy-agnostic, supports 1–4 leg trades |
| **Strategy router** | `config/strategies.toml` maps tickers → strategy names; `src/router.py` resolves + builds the right strategy instance per ticker |
| **Per-strategy state** | `state/{strategy}.json` layout via `get_strategy_state()`/`set_strategy_state()` — different strategies don't share state files |
| **Multi-leg executor** | `OrderIntent` + `execute()` in `order_executor.py` routes single equity, single option, or 2–4 leg combos to the correct Alpaca API |
| **Pluggable backtester** | `Strategy.simulate_trade(card, future_ohlcv) → TradeResult` Protocol — each strategy owns its P&L model; BS math lives in `wheel/backtester.py` |
| **DataAdapter split** | `DataAdapter` (all strategies) + `OptionsDataAdapter` (options strategies) with `get_multi_ohlcv`, `get_quote`, `get_option_quote` |
| **Filter Protocol** | All 7 Wheel filters implement `run(market_data, state, config) → FilterResult` — swappable without touching `WheelStrategy` |
| **Env guards** | `AlpacaPaperAdapter` raises a clear `RuntimeError` on missing `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` via `_require_env()` |
| **Deterministic fixtures** | `FixtureAdapter` uses a pinned `FIXTURE_REFERENCE_DATE` constant instead of `date.today()` — DTE calculations never drift |
| **Structured logging** | `structlog` wired across all modules — every filter result, signal emitted, and order placed is logged at `debug`/`info` level |

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
| **Dashboard UX hardening** | ✅ Complete | Alpaca as default adapter, View Details navigation, sticky sidebar page state, adapter-aware ticker dropdowns, fixture-availability filtering |
| **Track A — Architecture refactor** | ✅ Complete | Multi-strategy foundation: generalized SignalCard + Leg schema, Strategy.emit_signal_card Protocol, per-strategy state store, DataAdapter/OptionsDataAdapter split, strategy router (config/strategies.toml), OrderIntent multi-leg executor, Strategy.simulate_trade backtester split — 198 tests passing |
| **Track B — Put Credit Spreads** | 🔄 In progress | Defined-risk 2-leg option spreads on SPY/QQQ/IWM; multi-leg Alpaca orders |
| **Track B — RSI(2) ETF basket** | ⏳ Upcoming | Cross-sectional mean-reversion on 15-ETF universe; equity-only, daily cron |
