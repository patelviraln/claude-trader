# claude-trader — Enhancement Roadmap

A prioritized, implementable backlog of features that would take the current system from "generates signals and places paper orders" to a complete personal options income platform with proper risk management, intelligence, and operational maturity.

Each phase is scoped to roughly one focused work session and lists concrete deliverables, files affected, and acceptance criteria.

---

## Current state (baseline)

Phases 1–7 complete. The system can:
- Run the 7-filter Wheel pipeline on live or fixture data
- Place Alpaca paper orders for SELL_PUT signals
- Generate AI thesis via Claude API
- Backtest with synthetic IV and Black-Scholes pricing
- Serve a Streamlit dashboard with 5 pages

**Gaps that block real-world usefulness:**
1. No position tracking — once an order is placed, the system forgets
2. No P&L visibility — can't tell which signals made money
3. No exit logic — no profit-taking, no 21-DTE rolls, no stop-losses
4. No risk management — no buying-power check, no concentration limits
5. No event awareness — happy to sell a put 2 days before earnings
6. No alerting — must manually check the dashboard
7. Single strategy — Wheel only, no spreads or condors

---

## Phase 8 — Position Management & Mark-to-Market

**Goal:** Track every open option position from entry through exit. Show live P&L.

**Why first:** Everything downstream (risk, exit rules, performance attribution) depends on knowing what positions are open and what they're worth right now.

**Deliverables:**
- `src/positions.py` — `PositionStore` reading Alpaca live positions on every refresh
- `src/position_pnl.py` — mark-to-market: pull current option quote, compute unrealized P&L per leg
- New `positions.jsonl` log: every fill / close event with reason (signal_close, 21_dte, profit_target, assignment)
- New **Positions** page in the dashboard: open positions table, per-position P&L, days to expiry, % of max profit captured, "Close" button

**Files:**
- New: `src/positions.py`, `src/position_pnl.py`, `tests/unit/test_positions.py`
- Modify: `streamlit_app.py` (add `page_positions()`), `src/adapters/alpaca_adapter.py` (add `get_positions()` and `get_option_quote(symbol)`)

**Acceptance:**
- After placing a SELL_PUT order, the new position appears on the Positions page
- Mark-to-market refreshes show realistic unrealized P&L
- Closing a position via the dashboard submits a buy-to-close order and logs to `positions.jsonl`

**Estimated effort:** Medium — adds new module, no breaking changes

---

## Phase 9 — Exit Rules & Position Lifecycle

**Goal:** Automate the income side of the Wheel — close winners at 50% max profit, roll losers, exit at 21 DTE.

**Why:** Standard Wheel best practice. Letting puts expire worthless captures all premium but wastes time-decay opportunity on positions already up 70%+.

**Deliverables:**
- `src/exit_engine.py` — daily evaluation of open positions:
  - **Profit target:** close if unrealized P&L ≥ 50% of premium collected
  - **21 DTE rule:** close any short position with ≤ 21 DTE remaining (whether winner or loser)
  - **Stop-loss:** close if option price ≥ 2× premium collected (configurable)
- New `[exit_rules]` section in `config/wheel.toml`
- Schedule integration: `scheduler.py` runs exits before new entries each morning

**Files:**
- New: `src/exit_engine.py`, `tests/unit/test_exit_engine.py`
- Modify: `config/wheel.toml`, `scheduler.py`, `streamlit_app.py` (Positions page shows "exit reason" badge)

**Acceptance:**
- Backtest result includes both opening and closing trades with correct P&L attribution
- Scheduler logs show "exit_triggered" events with reason (profit_target, 21_dte, stop_loss)
- Dashboard Positions page shows projected close date / reason for each open position

**Estimated effort:** Medium

---

## Phase 10 — Risk Management & Position Sizing

**Goal:** Size positions based on available capital, volatility, and concentration limits. Refuse to over-allocate.

**Why:** A signal without sizing logic is useless. Selling 10 cash-secured puts on NVDA at $200 strike requires $200,000 buying power per contract — you cannot just blindly trade every signal.

**Deliverables:**
- `src/risk_manager.py`:
  - **Position sizing:** % of buying power per signal (default 5%), capped by Kelly fraction estimate
  - **Concentration limits:** max N concurrent positions per ticker, max % capital per sector
  - **Buying power check:** query Alpaca account, refuse to place orders exceeding limits
  - **Per-signal max contracts** added to `SignalCard`
- New `[risk]` section in `config/wheel.toml`
- Risk panel on dashboard: account equity, BP used, BP available, position count, sector exposure
- Pre-order safety: `OrderExecutor.place_order_from_card()` calls `RiskManager.validate()` first

**Files:**
- New: `src/risk_manager.py`, `tests/unit/test_risk_manager.py`
- Modify: `src/order_executor.py`, `src/signal_output.py` (add `max_contracts` field), `streamlit_app.py` (risk panel)

**Acceptance:**
- An order that would exceed BP is rejected before submission, logged with the rule that blocked it
- Dashboard shows current BP utilization
- Backtest results account for limited capital (not infinite contracts)

**Estimated effort:** Medium-large — touches order path

---

## Phase 11 — Event & Calendar Awareness

**Goal:** Don't sell premium into earnings, FOMC, ex-dividend dates. Don't get blindsided by predictable volatility events.

**Why:** Selling a 30-DTE put 5 days before earnings is a recipe for blown trades. Wheel works best when you avoid known event risk.

**Deliverables:**
- `src/calendar_filter.py`:
  - **Earnings filter:** block entries if next earnings within N days (default 7); pull from Alpaca corporate actions or yfinance
  - **Dividend filter:** for Phase B (calls), block if ex-div date within DTE window (early-assignment risk)
  - **FOMC / CPI filter:** optional date-based block list in config
- New filter slot in the pipeline (filter #8, soft flag): `EarningsProximityFilter`
- Dashboard Ticker Detail page shows upcoming earnings / dividend / FOMC dates

**Files:**
- New: `src/calendar_filter.py`, `src/strategies/wheel/filters_calendar.py`, `tests/unit/test_calendar_filter.py`
- Modify: `src/strategies/wheel/strategy.py` (register new filter), `streamlit_app.py`

**Acceptance:**
- Running signals on a ticker with earnings tomorrow produces NO_SIGNAL with reason `earnings_proximity`
- Phase B signals respect ex-dividend dates
- Backtest can be configured to honor or ignore calendar filters (for apples-to-apples comparison)

**Estimated effort:** Medium — depends on a reliable earnings calendar source

---

## Phase 12 — Enhanced AI Research (Tool Use + RAG)

**Goal:** Use Claude as a research assistant, not just a thesis generator. Let it query company data, summarize news, surface similar historical setups.

**Why:** Right now `thesis_generator.py` writes a paragraph based on the signal alone. With tool use, Claude can pull live financials, recent news, analyst targets, and prior trade outcomes for the same ticker.

**Deliverables:**
- `src/research_agent.py` — Claude agent with tools:
  - `get_company_financials(ticker)` — pull from Alpaca or yfinance
  - `search_recent_news(ticker, days)` — Alpaca news endpoint or NewsAPI
  - `find_similar_past_signals(ticker, phase)` — RAG over `signals.jsonl` + `positions.jsonl`
- Use `claude-opus-4-7` with adaptive thinking, streaming, and the tool runner
- New dashboard page: **Research** — query Claude about any ticker, see tool calls and final analysis
- `--research` CLI flag on `run_signals.py` to inject a fuller research blob into the SignalCard

**Files:**
- New: `src/research_agent.py`, `src/research_tools.py`, `tests/unit/test_research_agent.py`
- Modify: `streamlit_app.py` (add `page_research()`), `run_signals.py`

**Acceptance:**
- Claude can answer "What were my last 5 NVDA trades and how did they perform?"
- Claude can pull NVDA's last earnings beat/miss and reflect it in the thesis
- All tool calls and outputs visible in the dashboard for auditability

**Estimated effort:** Large — touches Claude SDK integration and data sources

---

## Phase 13 — Advanced Backtesting

**Goal:** Make backtest results trustworthy enough to actually inform parameter choices.

**Why:** Current backtester uses 30-day realized vol as IV proxy, zero slippage, zero commissions. Results are directionally correct but quantitatively optimistic.

**Deliverables:**
- **Slippage model:** `bid-ask spread × fill_factor` (default 0.5 of spread)
- **Commission model:** $0.65/contract default, configurable
- **Realistic IV:** apply skew approximation — OTM puts trade at higher IV than ATM (Heston-lite or fixed skew curve)
- **Risk metrics:** Sharpe, Sortino, Calmar, max drawdown, win rate, profit factor, average days in trade
- **Walk-forward parameter optimization:** grid search over `delta_target`, `dte_target`, `rsi_min/max` with out-of-sample validation
- **Strategy comparison:** Wheel vs covered-call-only vs buy-and-hold on same date range
- **Monte Carlo:** resample trade sequence to get confidence intervals on annualized return

**Files:**
- Modify: `src/backtester.py` (large refactor — split into `pricing.py`, `simulator.py`, `metrics.py`)
- New: `src/backtest_optimizer.py`, `tests/unit/test_backtest_metrics.py`
- Modify: `streamlit_app.py` (Backtest page adds metrics table, comparison toggle, optimizer button)

**Acceptance:**
- Backtest output includes Sharpe, max DD, profit factor (not just total P&L)
- Optimizer grid produces a parameter heatmap visualization
- Monte Carlo shows 95% CI on annualized return

**Estimated effort:** Large

---

## Phase 14 — Notifications & Alerting

**Goal:** Get notified when signals fire, orders fill, positions hit profit target, or anything fails.

**Why:** A daily-cron system that runs unattended needs to tell you what happened without you opening the dashboard.

**Deliverables:**
- `src/notifier.py` — pluggable sinks:
  - **Email** (SMTP via env vars)
  - **Slack webhook**
  - **Discord webhook**
  - **SMS via Twilio** (optional)
- Event types:
  - `signal_fired` — new actionable signal
  - `order_filled` — broker confirms execution
  - `assignment` — put assigned, position transitions A → B
  - `exit_triggered` — position closed (with reason and P&L)
  - `error` — pipeline failed for a ticker
- `[notifications]` config section: per-event sink selection
- Daily digest email at end of trading day with all activity + open positions snapshot

**Files:**
- New: `src/notifier.py`, `src/sinks/` (one file per channel), `tests/unit/test_notifier.py`
- Modify: `src/signal_engine.py`, `src/order_executor.py`, `src/exit_engine.py`, `scheduler.py`

**Acceptance:**
- Configuring Slack webhook and running a scan posts a card to the channel
- Order fills trigger a notification within 30s
- Daily digest email arrives at 4:30 PM ET with the day's summary

**Estimated effort:** Medium — mostly integration plumbing

---

## Phase 15 — Multi-Strategy Framework

**Goal:** Run multiple option-income strategies in parallel under the same pipeline — Wheel, vertical credit spreads, iron condors.

**Why:** The Wheel is great for trending stocks. Iron condors are better for range-bound. Verticals reduce capital required. A real income engine uses the right tool per ticker / regime.

**Deliverables:**
- The `Strategy` Protocol and `registry` already exist — extend them
- New strategies:
  - `CashSecuredPutStrategy` (rename current Wheel Phase A as standalone)
  - `CoveredCallStrategy` (Phase B standalone)
  - `PutCreditSpreadStrategy` (defined-risk version of Phase A)
  - `IronCondorStrategy` (for low-IV-rank, range-bound)
- Strategy router: per-ticker config selects which strategy to run
- Capital allocation across strategies (e.g., 60% Wheel, 30% spreads, 10% condors)
- Backtest can run any strategy

**Files:**
- New: `src/strategies/spreads/`, `src/strategies/condor/`, `src/strategies/router.py`
- Modify: `config/wheel.toml` → `config/strategies.toml`, `run_signals.py`, `streamlit_app.py`

**Acceptance:**
- A ticker tagged for "spread" strategy emits a `SELL_PUT_SPREAD` signal instead of `SELL_PUT`
- Backtest can compare Wheel vs Spread on the same ticker
- Dashboard shows per-strategy P&L attribution

**Estimated effort:** Large — architectural

---

## Phase 16 — Production Infrastructure

**Goal:** Move off "personal laptop running APScheduler" to something that won't break if the laptop sleeps.

**Why:** Reliability. The current setup loses runs whenever your machine is off, sleeps, or you're on the road.

**Deliverables:**
- **Persistence:** migrate `wheel_state.json`, `signals.jsonl`, `iv_history/`, `positions.jsonl` → SQLite (or PostgreSQL for hosted)
- **Dockerfile** + `docker-compose.yml` for local containerized runs
- **GitHub Actions** workflow that runs `python run_signals.py --execute` on cron (only viable on free tier — checkin tracking via commits)
- Alternative: **Fly.io / Railway / Render** deployment with persistent volume
- **FastAPI backend** (port 8000) — read-only API the Streamlit dashboard consumes; allows mobile/other clients
- **Health endpoint** `/health` for uptime monitoring

**Files:**
- New: `src/db.py`, `src/api/` (FastAPI app), `Dockerfile`, `docker-compose.yml`, `.github/workflows/daily.yml`
- Modify: all modules that read/write JSON files → use repo layer instead

**Acceptance:**
- All state in SQLite, no JSON files in repo root
- `docker compose up` brings up scheduler + API + dashboard
- Daily run executes regardless of laptop state (cloud or GitHub Actions)
- Dashboard reads via API, not direct file access

**Estimated effort:** Large

---

## Phase 17 — Live Trading Safety Harness

**Goal:** Add the safety rails required before flipping `ALPACA_BASE_URL` from paper to live.

**Why:** Paper accounts forgive mistakes. Live accounts do not.

**Deliverables:**
- **Mode flag:** `[trading] mode = "paper"|"live"` in config; live mode requires explicit `--live` CLI flag + interactive confirmation
- **Daily loss limit:** halt all trading if realized P&L < -$X for the day
- **Per-trade size cap** (hard ceiling, not just risk manager soft limit)
- **Reconciliation:** every run, diff local position state vs Alpaca; alert on mismatch
- **Trade journal:** every order writes a structured record with: signal_id, reasoning, filters that passed, thesis, projected outcome, actual outcome
- **Audit log:** append-only `audit.jsonl` recording every state change, every order, every override
- **Kill switch:** dashboard button + CLI command to cancel all open orders and exit all short option positions

**Files:**
- New: `src/safety.py`, `src/reconcile.py`, `src/audit.py`, `src/kill_switch.py`
- Modify: `src/order_executor.py`, all entry points

**Acceptance:**
- Switching to `mode = "live"` requires confirming a 6-digit code shown only in logs
- Kill switch tested in paper mode actually closes all open shorts
- Reconciliation catches a manually-placed Alpaca order and surfaces it as a discrepancy
- Audit log has every state-changing operation since Phase 17 was enabled

**Estimated effort:** Large — touches every entry point

---

## Suggested ordering rationale

The fastest path to a system you can actually run on real capital:

1. **Phase 8 (Position Management)** — prerequisite for everything else
2. **Phase 9 (Exit Rules)** — makes the Wheel actually a Wheel, not just an entry-signal generator
3. **Phase 10 (Risk & Sizing)** — required before any live capital touches it
4. **Phase 11 (Calendar Filters)** — cheap insurance against avoidable losses
5. **Phase 14 (Notifications)** — operational maturity before going live
6. **Phase 17 (Safety Harness)** — last gate before live mode
7. **Phase 13 (Advanced Backtesting)** — informs parameter choices once everything else is solid
8. **Phase 12 (AI Research)** — quality-of-life upgrade; not on the critical path
9. **Phase 15 (Multi-Strategy)** — once Wheel is performing well, expand
10. **Phase 16 (Production Infra)** — only worth doing once the strategy is profitable

Phases 8–10 alone would take the system from "signal generator" to "complete Wheel execution engine." Phases 14 + 17 then make it safe to deploy with real money.

---

## Out-of-scope / explicitly not doing

- **HFT or sub-second strategies** — Alpaca free tier and the daily-cron architecture aren't suited
- **Hosting client funds / multi-user** — personal-use system; SOC2 / fiduciary concerns are not in scope
- **Equity strategies beyond options income** — keep the system focused
- **Closed-source data feeds (Bloomberg, Refinitiv)** — relying on free tiers (Alpaca IEX, yfinance, NewsAPI free)
