# claude-trader

A personal AI-assisted **multi-strategy paper trading system** for US equities and options. It scans daily, places paper orders via Alpaca, tracks open positions with live P&L, closes them by rule, records realized P&L per strategy, and reports to you — all from a Streamlit dashboard and a single daily scheduler run.

**Three strategies, one router:**

| Strategy | Trade | Signal types |
|---|---|---|
| `wheel` | Cash-secured puts → covered calls after assignment | `SELL_PUT`, `SELL_CALL` |
| `put_credit_spread` | Defined-risk bull put spreads (~0.30Δ short / ~0.15Δ long) | `SELL_PUT_SPREAD` |
| `rsi2` | Connors RSI(2) mean-reversion on ETFs, long-only above the 200-DMA | `BUY_EQUITY`, `SELL_EQUITY` |

`config/strategies.toml` maps each ticker to its strategy; unassigned tickers fall back to the default.

---

## Wheel modes: Sell Put / Covered Call

The wheel runs a two-mode state machine per ticker (stored internally as phase `"A"`/`"B"` in `wheel_state.json`; the UI and CLI always show the names):

- **Sell Put** (`A`) — no shares held; sell cash-secured puts to collect premium or get assigned at a discount. One open put per ticker at a time (duplicate-entry guard).
- **Covered Call** (`B`) — 100 shares held after assignment; sell covered calls against them.

Transitions happen three ways: automatically (the reconciler detects ≥100 shares and flips to Covered Call), from the dashboard (Wheel Mode form on Ticker Detail), or via CLI:

```bash
python phase_transition.py assign NVDA --cost-basis 880.50   # Sell Put -> Covered Call
python phase_transition.py exit NVDA                          # Covered Call -> Sell Put
```

---

## Requirements

- Python 3.11+
- An [Alpaca](https://alpaca.markets) paper trading account (free)
- Optional: an [OpenRouter](https://openrouter.ai/keys) key for AI thesis generation

```bash
pip install -e ".[dev]"
```

## Setup

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | For live data, orders, positions | Alpaca paper credentials |
| `ALPACA_BASE_URL` | With the above | `https://paper-api.alpaca.markets` |
| `OPENROUTER_API_KEY` | Optional | Enables AI thesis (`--thesis`) |
| `SCANNER_MODEL` | Optional | Primary thesis model (default `openai/gpt-oss-120b:free`) |
| `SCANNER_FALLBACK_MODEL` | Optional | Tried when the primary errors; `""` disables |
| `NTFY_TOPIC` | Optional | Daily-summary push via [ntfy.sh](https://ntfy.sh); unset → `logs/daily_summary.md` |

Fixture-only mode (`--adapter fixture`) needs **no keys**.

Configs: `config/strategies.toml` (router, scheduler, exit rules), `config/wheel.toml`, `config/put_credit_spread.toml`, `config/rsi2.toml` — all editable from the dashboard's Configuration page.

---

## The daily loop

One scheduler run executes the full lifecycle, in order:

```
1. Reconcile   — sync strategy state with live positions (fills, assignments, closes)
2. Exits       — close short options by rule: 50% profit target, 21 DTE, 2x stop-loss
3. Scans       — route each ticker to its strategy, emit signals; with auto_execute,
                 place paper orders — every order risk-gated first (Phase 10)
4. Fill ledger — persist new fills to fills.jsonl (realized P&L source of truth)
5. Summary     — push/write a run report (signals, orders, risk blocks, exits, P&L)
```

```bash
python scheduler.py --run-now     # one full run, immediately
python scheduler.py               # blocking cron loop (Mon-Fri at [scheduler] time)
```

**Unattended daily runs** (Windows Task Scheduler, Mon–Fri, logged to `logs/scheduler_runs.log`):

```powershell
.\scripts\register_daily_task.ps1              # default 14:35 local = 9:35 ET during US DST
.\scripts\register_daily_task.ps1 -Time 15:35  # adjust when DST shifts
.\scripts\register_daily_task.ps1 -Remove
```

Risk limits and exit rules live in `config/strategies.toml` (editable from the
dashboard's **Risk & Exits** tab):

```toml
[risk]                             # enforced before EVERY order (Phase 10)
enabled                  = true
max_position_pct         = 5.0    # capital per new position, % of account equity
max_positions_total      = 10     # max concurrent open positions
max_positions_per_ticker = 1      # max per underlying
min_buying_power_pct     = 20.0   # % of equity kept free after any new trade

[exit_rules]
enabled            = true
profit_target_pct  = 50.0   # close when >= this % of premium has decayed
dte_close          = 21     # close anything at/under this many days to expiry
stop_loss_multiple = 2.0    # close when mark >= this multiple of premium collected

[scheduler]
auto_execute = true          # daily runs place risk-gated paper orders
```

Capital requirements per signal: cash-secured puts reserve `strike x 100`;
spreads reserve their defined max loss; equity buys reserve notional; covered
calls and closing trades are always allowed. Rejected orders are recorded on
the signal card (`risk_blocked: ...`) and in the daily summary — never placed.

---

## Ad-hoc scans (CLI)

```bash
python run_signals.py --tickers NVDA TLT --adapter fixture        # auto-routed per ticker
python run_signals.py --tickers SPY --strategy put_credit_spread  # force a strategy
python run_signals.py --tickers MSFT --adapter alpaca --execute   # place paper orders
python run_signals.py --tickers NVDA --adapter alpaca --thesis    # AI thesis via OpenRouter
```

```
--tickers       One or more ticker symbols (required)
--adapter       fixture | alpaca  (default: fixture)
--strategy      wheel | put_credit_spread | rsi2 — omit to auto-route via strategies.toml
--phase         A | B — override wheel mode for all tickers this run
--config        Path to wheel config (default: config/wheel.toml)
--state         Path to wheel state (default: wheel_state.json)
--signals-out   JSONL output path (default: signals.jsonl)
--json-only     Raw JSON cards, no rich display
--thesis        AI thesis (requires OPENROUTER_API_KEY)
--execute       Place Alpaca paper orders for actionable signals
```

---

## Dashboard

```bash
python -m streamlit run streamlit_app.py    # http://localhost:8501
```

Six pages (`st.navigation`, per-page URLs, mtime-cached data layer, fragment-isolated forms):

| Page | What it shows |
|---|---|
| **Dashboard** | Ticker cards with strategy badges, wheel mode (Sell Put / Covered Call), last signal, price, IV rank / RSI(2), confidence. Strategy filter + optional 30s auto-refresh. |
| **Positions** | Account panel (equity, cash, buying power), live open positions with unrealized P&L, DTE, % of max profit, exit-rule status, manual Close button, **realized P&L per strategy** from the fill ledger, position event feed, state-sync button. |
| **Ticker Detail** | Strategy badge, per-strategy state metrics, IV history chart (options strategies), signal history table, Wheel Mode transition form. |
| **Run Signals** | Strategy selector (auto-route or forced), adapter choice, scan + optional paper orders / thesis from the UI. |
| **Backtest** | All three strategies: wheel walk-forward (BS pricing), RSI(2) rolling-window walk-forward, simplified PCS spread simulation. Equity curve + trade table. |
| **Configuration** | Per-strategy parameter editors (wheel / PCS / RSI2 TOMLs), **Router** tab (ticker → strategy assignments), scheduler settings, tracked-ticker management. |

---

## Data files (the audit trail)

| File | What it records |
|---|---|
| `signals.jsonl` | Every signal card ever emitted (strategy-agnostic schema) |
| `positions.jsonl` | Position lifecycle events: exit closes (with rule + reason), manual closes |
| `fills.jsonl` | Every filled order (deduped) — source of truth for realized P&L |
| `wheel_state.json` | Wheel per-ticker mode state (legacy path) |
| `state/{strategy}.json` | Per-strategy state (rsi2 in-position, PCS open-spread) |
| `iv_history/{TICKER}.jsonl` | Rolling ATM IV samples for IV rank |
| `logs/daily_summary.md` | Daily run summaries (when `NTFY_TOPIC` unset) |

Realized P&L = matched buy/sell round-trips per symbol (×100 for options), rolled up per strategy. Visible on the Positions page and in the daily summary.

---

## Signal card schema

Strategy-agnostic: option fields live in `legs[]`, strategy extras in `payload{}`.

```json
{
  "strategy_name": "wheel",
  "ticker": "MSFT",
  "signal_type": "SELL_PUT",
  "underlying_price": 422.00,
  "legs": [{
    "asset_class": "option", "symbol": "MSFT", "side": "sell", "qty": 1,
    "order_type": "limit", "limit_price": 7.86, "strike": 405.0,
    "expiry": "2026-06-18", "option_type": "put", "delta_estimate": -0.314
  }],
  "indicators": { "ema50": 404.51, "rsi14": 58.3, "bb_percent_b": 0.65 },
  "payload": { "dte": 33, "iv_rank": 100.0 },
  "confidence_tier": "MEDIUM",
  "confidence_rationale": "Soft flag: bb_unfavorable",
  "order_ids": ["a0c8dc2b-..."]
}
```

An `rsi2` card has one equity leg (`qty` = shares, no strike/expiry) and `indicators: {rsi2, sma200, close}`. A `put_credit_spread` card has two option legs (short + long put) and `payload: {net_credit, spread_width, max_profit, max_loss}`.

Confidence tiers: **HIGH** (clean pass), **MEDIUM** (1 soft flag / imprecise delta / IV rank missing), **LOW** (2+ soft flags or delta at tolerance edge).

---

## Filter pipelines

**Wheel** (7 filters): Mode Gate (valid mode, no duplicate open put) → EMA-50 trend → RSI(14) 35–65 → Bollinger %B (soft) → options chain → ±0.30Δ strike (±0.05) → volume-profile anchor (soft).

**Put credit spread** (6): Spread-open gate → EMA → RSI → IV-rank ≥ 25 → chain → dual-leg builder (0.30Δ/0.15Δ, min credit, max width).

**RSI(2)** (4): Position gate → close > SMA(200) → RSI(2) < 10 → position sizer (5% of equity). Exits: RSI(2) > 70 or 5-day max hold.

All filters implement `run(market_data, state, config) → FilterResult` (`src/strategies/base.py`).

---

## AI thesis (optional)

`--thesis` (or the dashboard checkbox) sends the signal snapshot + recent IV history to a model on **OpenRouter** and attaches a one-sentence rationale. Models are set entirely in `.env` (`SCANNER_MODEL`, `SCANNER_FALLBACK_MODEL`); the fallback fires automatically on errors, with 429 retry+backoff for free-tier models. No code changes needed to swap models.

---

## Tests

```bash
pytest    # 337 tests
```

Coverage spans: all strategy filter chains and signal emission (wheel / PCS / RSI2), the router, per-strategy state store, reconciliation (fill→state sync, assignment detection, duplicate-entry guard), exit engine (all three rules, close+log flow), positions (OCC parsing, P&L math, event log), fill ledger (round-trip realized P&L, dedupe), order executor (single-leg, multi-leg, equity), adapters (mocked Alpaca SDK), backtesters, scheduler dispatch (routed + legacy), thesis generator (mocked HTTP), and the dashboard data layer.

---

## Project structure

```
claude-trader/
├── run_signals.py              # CLI scan (auto-routes via strategies.toml)
├── streamlit_app.py            # dashboard (6 pages, st.navigation, cached data layer)
├── scheduler.py                # daily pipeline: reconcile → exits → scans → fills → summary
├── phase_transition.py         # wheel mode CLI (Sell Put <-> Covered Call)
├── config/
│   ├── strategies.toml         # router assignments + scheduler + exit rules
│   ├── wheel.toml / put_credit_spread.toml / rsi2.toml
├── scripts/
│   ├── register_daily_task.ps1 # Windows Task Scheduler registration
│   └── migrate_*.py
├── fixtures/                   # deterministic test data (incl. TLT rsi2 scenario)
├── src/
│   ├── router.py               # ticker → strategy resolution + build
│   ├── signal_engine.py        # orchestrator (strategy_name: None | "auto" | name)
│   ├── signal_output.py        # Leg + SignalCard schema, rich formatter, JSONL log
│   ├── positions.py            # Position model, OCC parser, event log      (Phase 8)
│   ├── position_pnl.py         # DTE, % max profit, quote-based marking     (Phase 8)
│   ├── exit_engine.py          # profit-target / DTE / stop-loss exits      (Phase 9)
│   ├── reconcile.py            # fill-to-state sync, assignment detection
│   ├── pnl_ledger.py           # fills.jsonl + realized P&L per strategy
│   ├── notifier.py             # daily summary (ntfy.sh push or file)
│   ├── order_executor.py       # OrderIntent execute(), positions, account, close
│   ├── thesis_generator.py     # OpenRouter thesis (env-driven models)
│   ├── backtester.py / indicator_engine.py / iv_history.py / state_store.py
│   ├── adapters/               # DataAdapter Protocol: fixture + Alpaca (IEX)
│   └── strategies/
│       ├── base.py / registry.py
│       ├── wheel/              # filters, state, strategy, BS backtester
│       ├── spreads/            # PutCreditSpreadStrategy
│       └── momentum/           # RSI2Strategy
└── tests/                      # 337 tests (unit + integration)
```

---

## Roadmap

| Milestone | Status |
|---|---|
| Wheel signal pipeline, live data, orders, dashboard, backtest | ✅ |
| Track A — multi-strategy architecture (SignalCard/Leg, router, per-strategy state, OrderIntent) | ✅ |
| Track B — Put Credit Spreads + RSI(2) strategies | ✅ |
| Dashboard performance overhaul (cached data layer, st.navigation, fragments) | ✅ |
| Phase 8 — position tracking & mark-to-market | ✅ |
| Phase 9 — exit rules (50% PT / 21 DTE / 2× SL) | ✅ |
| Loop hardening — reconciliation, realized P&L ledger, daily summary, task scheduling | ✅ |
| Phase 10 — risk management, position sizing, risk-gated auto-execution | ✅ |
| Phase 11 — event/calendar awareness (earnings, FOMC, ex-div) | next |
| Phase 13 — generic multi-strategy backtest engine with capital constraints | planned |
| Phase 14 — full notifications; Phase 16 — production infra; Phase 17 — live trading harness | planned |

See `ENHANCEMENT.md` for the detailed phase specs.
