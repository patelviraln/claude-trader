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
0. **Architecture is Wheel-bound in practice** — the `Strategy` Protocol and registry exist, but the orchestrator (`signal_engine.py`), output schema (`SignalCard`), executor (single-leg OCC), adapter Protocol (options-centric), backtester (BS + phase A/B), scheduler, config, dashboard, and state model all hardcode Wheel concepts. See **Track A** below.
1. No position tracking — once an order is placed, the system forgets
2. No P&L visibility — can't tell which signals made money
3. No exit logic — no profit-taking, no 21-DTE rolls, no stop-losses
4. No risk management — no buying-power check, no concentration limits
5. No event awareness — happy to sell a put 2 days before earnings
6. No alerting — must manually check the dashboard
7. Single strategy — Wheel only, no spreads or condors

---

## Track A — Architecture Refactor (DO FIRST, ~1 focused week)

**Why this comes before everything else:** The existing Phase 15 (Multi-Strategy Framework) claim that "the Strategy Protocol and registry already exist, just extend them" is wishful thinking. An architecture audit found that `signal_engine.py`, `SignalCard`, `OrderExecutor`, the adapter Protocol, the backtester, the scheduler, the dashboard, the state model, and `config/wheel.toml` all hardcode Wheel-specific data shapes. Adding a second strategy without this refactor will either:
- Force the second strategy to mimic Wheel's shape (single-leg, options-only, "phase A/B"), making it a fork rather than a peer, OR
- Break the SignalCard schema and the JSONL signal log mid-flight, which then breaks every downstream consumer (dashboard, backtester, executor).

**Track A goal:** Make the codebase actually strategy-pluggable so Track B (Put Credit Spreads, RSI(2), and future strategies) is genuinely a "drop in a new class + config" exercise.

**Important:** Do **not** start Phase 16 (Production Infrastructure — SQLite/FastAPI) until Track A is complete. Otherwise you bake Wheel-shaped schemas into the database and pay for the migration twice.

### Phase summary

| # | Name | Effort | Prerequisites | Breaking changes |
|---|---|---|---|---|
| A1 | Generalize SignalCard + introduce Leg | Medium | none | `signals.jsonl` schema; SignalCard fields |
| A2 | Strategy emits SignalCard; gut orchestrator | Medium | A1 | `signal_engine.run_signal()` signature |
| A3 | Per-strategy state store | Small | A1, A2 | `wheel_state.json` → `state/wheel.json`; state_store API |
| A4 | DataAdapter Protocol extensions + capability split | Small | A1 | `DataAdapter` Protocol (additive); `OptionsDataAdapter` new |
| A5 | Per-strategy config + scheduler router | Small | A1, A2, A3 | `config/strategies.toml` new; scheduler reads router |
| A6 | OrderIntent abstraction in executor | Medium | A1 | `OrderExecutor` API; supports multi-leg + equity |
| A7 | Split backtester per-strategy | Medium | A1, A2 | Wheel backtest moves to `strategies/wheel/backtester.py` |
| A8 | Dashboard strategy-awareness | Small | A1, A2, A3, A5 | `streamlit_app.py` constants removed; pages strategy-aware |

**Total effort:** ~5 focused days of coding + 1 day of test/migration work. Each phase is one pull request.

**Test count expectation:** 168 tests today. Expect to temporarily drop to ~140 during the refactor as Wheel-specific tests are rewritten; target 180+ after Track A complete (each generic contract adds its own test module).

---

### Phase A1 — Generalize SignalCard + introduce Leg model

**Goal:** Replace the single-leg, Wheel-specific `SignalCard` with a strategy-agnostic card supporting multi-leg orders and strategy-specific payloads. This is the *keystone* — every later phase depends on this schema.

**Prerequisites:** none — start here.

**Contracts to introduce (paste-ready):**

```python
# src/signal_output.py
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field

class Leg(BaseModel):
    """One leg of a trade. May be equity (1 leg) or option (1–4 legs)."""
    asset_class: Literal["equity", "option"]
    symbol: str  # OCC symbol for options, ticker for equity
    side: Literal["buy", "sell"]
    qty: int  # contracts for options, shares for equity
    order_type: Literal["market", "limit"] = "limit"
    limit_price: float | None = None  # required when order_type == "limit"
    # Option-specific (optional — populated when asset_class == "option"):
    strike: float | None = None
    expiry: str | None = None  # YYYY-MM-DD
    option_type: Literal["put", "call"] | None = None
    delta_estimate: float | None = None

class SignalCard(BaseModel):
    strategy_name: str  # e.g. "wheel", "put_credit_spread", "rsi2"
    ticker: str  # underlying / primary symbol
    signal_type: str  # strategy-defined; e.g. "SELL_PUT", "SELL_PUT_SPREAD", "BUY_EQUITY", "NO_SIGNAL"
    signal_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    underlying_price: float
    legs: list[Leg] = Field(default_factory=list)  # empty when signal_type == "NO_SIGNAL"
    indicators: dict[str, float] = Field(default_factory=dict)  # e.g. {"ema50": 422.5, "rsi14": 58.3, "bb_percent_b": 0.65}
    payload: dict[str, Any] = Field(default_factory=dict)  # strategy-specific extras: iv_rank, dte, hvn_price, spread_width, max_profit, ...
    thesis_text: str = ""
    confidence_tier: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    confidence_rationale: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    no_signal_reason: str | None = None
    order_ids: list[str] = Field(default_factory=list)  # one per leg if executed
    order_statuses: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json()
```

**Field migration map (old → new):**

| Old field | New location |
|---|---|
| `phase` (Literal SELL_PUT/SELL_CALL/NO_SIGNAL) | `signal_type` (str, free-form per strategy) |
| `recommended_strike` | `legs[0].strike` |
| `recommended_expiry` | `legs[0].expiry` |
| `dte` | `payload["dte"]` |
| `delta_estimate` | `legs[0].delta_estimate` |
| `option_mid` | `legs[0].limit_price` |
| `iv_rank` | `payload["iv_rank"]` |
| `indicator_readings` (typed object) | `indicators` (dict[str, float]) |
| `order_id` (single str) | `order_ids` (list[str], one per leg) |
| `order_status` (single str) | `order_statuses` (list[str]) |

**Deliverables:**
- Rewrite `SignalCard` and add `Leg` in `src/signal_output.py`
- Update `print_signal_card()` to render multi-leg signals (one row per leg) and read `indicators`/`payload`
- Update `append_signal_jsonl()` — no structural change needed since it just calls `to_json()`
- Update `WheelStrategy` (in Phase A2) to populate the new shape
- One-shot migration script that rewrites the existing `signals.jsonl` to the new schema

**Files to modify:**
- `src/signal_output.py` (full rewrite of SignalCard; add Leg)
- `src/strategies/wheel/strategy.py` (will populate new schema — done in A2)
- `src/signal_engine.py` (will assemble new schema — done in A2)

**Files to create:**
- `scripts/__init__.py` (if it doesn't exist)
- `scripts/migrate_signals_jsonl.py` — reads `signals.jsonl`, writes to `signals.jsonl.new` in new schema; backs up old file as `signals.jsonl.legacy`

**Test plan:**
- New: `tests/unit/test_signal_card_v2.py` — roundtrip Leg + SignalCard via JSON; assert defaults; assert NO_SIGNAL emits empty legs
- New: `tests/unit/test_migrate_signals_jsonl.py` — feed sample old records, assert new shape
- Update (in A2): `tests/integration/test_fixture_scenarios.py` to assert new field paths
- Update: `tests/unit/test_dashboard_data.py` to read new shape

**Acceptance:**
- `python -c "from src.signal_output import SignalCard, Leg; print(SignalCard(strategy_name='wheel', ticker='SPY', signal_type='NO_SIGNAL', underlying_price=400.0).to_json())"` prints valid JSON
- `python scripts/migrate_signals_jsonl.py` produces `signals.jsonl.new` with one valid record per input line
- Old `signals.jsonl` preserved as `signals.jsonl.legacy`

---

### Phase A2 — Strategy emits SignalCard; gut signal_engine.py of Wheel-isms

**Goal:** `signal_engine.run_signal()` becomes a pure orchestrator that knows nothing about IV ranks, deltas, phases, or any Wheel-specific concept. The strategy itself produces a fully-formed `SignalCard`.

**Prerequisites:** A1

**Contracts:**

```python
# src/strategies/base.py
from typing import Any, Protocol
from pydantic import BaseModel
from src.signal_output import SignalCard
from src.adapters.base import DataAdapter

class Strategy(Protocol):
    name: str

    def load_config(self, config: dict[str, Any]) -> None: ...
    def get_state_schema(self) -> type[BaseModel]: ...

    def emit_signal_card(
        self,
        ticker: str,
        adapter: DataAdapter,
        state: dict[str, Any],
    ) -> SignalCard:
        """Run the full evaluation pipeline and produce a strategy-shaped SignalCard.
        This includes fetching data via adapter, running filters, assembling indicators
        and payload, computing confidence tier, and building leg(s). NO orchestrator
        post-processing required.
        """
        ...

    # Keep evaluate() as an internal method called by emit_signal_card.
    def evaluate(
        self,
        ticker: str,
        ohlcv,  # pd.DataFrame
        options_chain,  # pd.DataFrame | None
        state: dict[str, Any],
    ) -> list:  # list[FilterResult]
        ...
```

**New `signal_engine.py` target shape (~30 lines):**

```python
def run_signal(
    ticker: str,
    strategy: Strategy,
    adapter: DataAdapter,
    state_path: Path = Path("state"),
    signals_path: Path = Path("signals.jsonl"),
    print_output: bool = True,
    generate_thesis: bool = False,
    execute: bool = False,
    executor: OrderExecutor | None = None,
) -> SignalCard:
    state = get_strategy_state(strategy.name, ticker, state_path)
    card = strategy.emit_signal_card(ticker, adapter, state)

    if generate_thesis and card.signal_type != "NO_SIGNAL":
        from src.thesis_generator import generate_thesis as _gen
        card.thesis_text = _gen(card)

    if execute and executor is not None and card.legs:
        intent = OrderIntent(strategy_name=strategy.name, ticker=ticker, legs=card.legs)
        results = executor.execute(intent)
        card.order_ids = [r.order_id for r in results]
        card.order_statuses = [r.status for r in results]

    if print_output:
        print_signal_card(card)
    append_signal_jsonl(card, signals_path)
    return card
```

**Deliverables:**
- Add `emit_signal_card` to the `Strategy` Protocol
- Move all of `signal_engine.py` lines 31–188 (IV sampling, indicator readings assembly, phase-to-signal-type mapping, confidence tier, delta_estimate guard) into `WheelStrategy.emit_signal_card()` or a helper in `src/strategies/wheel/signal_builder.py`
- The Wheel implementation calls `evaluate()` internally to get FilterResults, then assembles the SignalCard

**Files to modify:**
- `src/strategies/base.py` — add `emit_signal_card` to Protocol
- `src/strategies/wheel/strategy.py` — implement `emit_signal_card`; pull in IV sampling and signal assembly logic
- `src/signal_engine.py` — gut to ~40 lines; remove ALL Wheel-specific imports/logic

**Files to create:**
- `src/strategies/wheel/signal_builder.py` (optional — only if WheelStrategy gets too big)

**Test plan:**
- All existing tests for `run_signal` should pass with no behavioral change for Wheel (output identical modulo field-rename from A1)
- New `tests/unit/test_signal_engine.py` — assert orchestrator no longer references "delta", "iv_rank", "phase", "SELL_PUT"
- Update `tests/integration/test_fixture_scenarios.py` to read new fields

**Acceptance:**
- `grep -E "(delta_estimate|iv_rank|phase|SELL_PUT|IndicatorReadings|compute_iv_rank|append_iv_sample)" src/signal_engine.py` returns ZERO hits
- `python run_signals.py --tickers WXYZ --adapter fixture` produces output equivalent to pre-refactor (cards.indicators contains ema50/rsi14/bb_percent_b; payload contains iv_rank/dte; legs[0] contains strike/expiry/delta)

---

### Phase A3 — Per-strategy state store

**Goal:** `wheel_state.json` becomes `state/wheel.json`; the orchestrator passes state through the strategy's own schema; `state["phase"]` is no longer accessed by the orchestrator.

**Prerequisites:** A1, A2

**Contracts:**

```python
# src/state_store.py
from pathlib import Path
from typing import Any

def get_strategy_state(
    strategy_name: str,
    ticker: str,
    base_path: Path = Path("state"),
) -> dict[str, Any]:
    """Read state for (strategy, ticker). Returns {} if not present."""

def set_strategy_state(
    strategy_name: str,
    ticker: str,
    state: dict[str, Any],
    base_path: Path = Path("state"),
) -> None:
    """Atomic write of state for (strategy, ticker)."""

def all_tickers(
    strategy_name: str,
    base_path: Path = Path("state"),
) -> list[str]:
    """List all tickers known to a given strategy."""
```

**Layout:**
```
state/
  wheel.json                # {"NVDA": {"phase": "A", ...}, "AAPL": {...}}
  put_credit_spread.json    # {"SPY": {"open_spread": false, ...}}
  rsi2.json                 # {"SPY": {"in_position": false, ...}}
```

**Deliverables:**
- New API in `src/state_store.py` (keep old `get_ticker_state`/`set_ticker_state` as thin shims with DeprecationWarning during migration)
- One-shot migration: `scripts/migrate_wheel_state.py` — read `wheel_state.json`, write to `state/wheel.json`, rename old file to `wheel_state.json.legacy`
- Update `phase_transition.py` and `streamlit_app.py` to use new API

**Files to modify:**
- `src/state_store.py` (add new API; keep old as shim)
- `src/signal_engine.py` (use `get_strategy_state(strategy.name, ticker)`)
- `phase_transition.py` (use new API with `strategy_name="wheel"`)
- `streamlit_app.py` (use new API; remove `STATE_PATH = "wheel_state.json"` constant in A8)

**Files to create:**
- `state/` directory (gitignored except `.gitkeep`)
- `scripts/migrate_wheel_state.py`

**Test plan:**
- Update `tests/unit/test_state_store.py` for the new API
- Migration test: feed sample wheel_state, assert content equivalence after script run

**Acceptance:**
- `state/wheel.json` exists and matches old `wheel_state.json` content byte-for-byte (modulo formatting)
- `python phase_transition.py assign NVDA --cost-basis 880.50` writes to `state/wheel.json`, not `wheel_state.json`
- Dashboard Assign/Exit buttons still work end-to-end

---

### Phase A4 — DataAdapter Protocol extensions + capability split

**Goal:** Extend the adapter Protocol with the methods equity-only / multi-symbol strategies need; split out an `OptionsDataAdapter` so non-options strategies don't depend on options methods.

**Prerequisites:** A1 (so SignalCard already supports equity legs)

**Contracts:**

```python
# src/adapters/base.py
from typing import Protocol
import pandas as pd
from datetime import date

class DataAdapter(Protocol):
    def get_ohlcv(
        self,
        ticker: str,
        bars: int,
        timeframe: str = "1Day",  # "1Min" | "5Min" | "15Min" | "1Hour" | "1Day"
    ) -> pd.DataFrame: ...

    def get_quote(self, ticker: str) -> dict:
        """Returns {"bid": float, "ask": float, "last": float, "ts": datetime}."""

    def get_historical_ohlcv(
        self,
        ticker: str,
        start: date,
        end: date,
        timeframe: str = "1Day",
    ) -> pd.DataFrame: ...

    def get_multi_ohlcv(
        self,
        tickers: list[str],
        bars: int,
        timeframe: str = "1Day",
    ) -> dict[str, pd.DataFrame]:
        """Batched fetch — single API call when adapter supports it."""

class OptionsDataAdapter(DataAdapter, Protocol):
    def get_options_chain(
        self,
        ticker: str,
        dte_min: int,
        dte_max: int,
    ) -> pd.DataFrame: ...

    def get_option_quote(self, occ_symbol: str) -> dict:
        """Returns {"bid": float, "ask": float, "last": float, "iv": float, "delta": float}."""
```

**Strategy type hints:**
- `RSI2Strategy.emit_signal_card(ticker, adapter: DataAdapter, state)` — equity only
- `WheelStrategy.emit_signal_card(ticker, adapter: OptionsDataAdapter, state)` — needs options
- `PutCreditSpreadStrategy.emit_signal_card(ticker, adapter: OptionsDataAdapter, state)` — needs options

**Deliverables:**
- Extend Protocol in `src/adapters/base.py`
- `AlpacaPaperAdapter`: implement `get_quote` (latest_quote endpoint), `get_multi_ohlcv` (StockBarsRequest with list of symbols), `get_option_quote` (latest_option_quote endpoint); add `timeframe` param to `get_ohlcv`/`get_historical_ohlcv`
- `FixtureAdapter`: stub all new methods (return synthetic quote based on latest OHLCV close; multi_ohlcv loops over single-symbol calls)

**Files to modify:**
- `src/adapters/base.py`
- `src/adapters/alpaca_adapter.py`
- `src/adapters/fixture_adapter.py`

**Test plan:**
- Update `tests/unit/test_alpaca_adapter.py` — mock new endpoints
- Add `get_multi_ohlcv` test (assert single API call, dict keyed by ticker)

**Acceptance:**
- `adapter.get_ohlcv("SPY", bars=10, timeframe="15Min")` returns 10 rows of 15-min bars from Alpaca
- `adapter.get_quote("SPY")` returns current bid/ask/last
- `adapter.get_multi_ohlcv(["SPY", "QQQ"], bars=200)` returns `{"SPY": df, "QQQ": df}`
- Wheel still works (its adapter param now typed as `OptionsDataAdapter` — same concrete class)

---

### Phase A5 — Per-strategy config + scheduler router

**Goal:** Replace the implicit "single Wheel config" assumption with an explicit per-ticker-to-strategy router.

**Prerequisites:** A1, A2, A3

**New `config/strategies.toml`:**

```toml
# Top-level router: who runs what
[router]
default_strategy = "wheel"  # used if a ticker has no explicit assignment

[router.assignments]
# Map ticker → strategy name
NVDA = "wheel"
AAPL = "wheel"
MSFT = "wheel"
AMZN = "wheel"
GOOGL = "wheel"
META  = "wheel"
TSLA  = "wheel"
INTC  = "wheel"
MU    = "wheel"
SPY  = "put_credit_spread"
QQQ  = "put_credit_spread"
IWM  = "put_credit_spread"
EEM  = "rsi2"
EFA  = "rsi2"
TLT  = "rsi2"
GLD  = "rsi2"

[scheduler]
adapter = "alpaca"
run_hour = 9
run_minute = 35
timezone = "America/New_York"

# Per-strategy config-file pointers
[strategies.wheel]
config_path = "config/wheel.toml"

[strategies.put_credit_spread]
config_path = "config/put_credit_spread.toml"

[strategies.rsi2]
config_path = "config/rsi2.toml"
```

**Router helper:**

```python
# src/router.py
import tomllib
from pathlib import Path
from src.strategies.registry import get as get_strategy_cls

def load_router(config_path: Path = Path("config/strategies.toml")) -> dict:
    with open(config_path, "rb") as f:
        return tomllib.load(f)

def resolve_strategy(ticker: str, router_cfg: dict) -> str:
    """Returns the strategy name for a ticker, with default fallback."""
    return router_cfg["router"]["assignments"].get(
        ticker, router_cfg["router"]["default_strategy"]
    )

def build_strategy(name: str, router_cfg: dict):
    """Instantiate + load_config for the strategy named `name`."""
    cls = get_strategy_cls(name)
    cfg_path = router_cfg["strategies"][name]["config_path"]
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    cfg = raw.get(name, raw)
    instance = cls()
    instance.load_config(cfg)
    return instance

def grouped_assignments(router_cfg: dict) -> dict[str, list[str]]:
    """Returns {strategy_name: [ticker, ...]} for batch dispatch."""
```

**Deliverables:**
- New `src/router.py`
- `scheduler.py` reads router, groups tickers by strategy, builds each strategy once, calls `run_signal` per (ticker, strategy)
- `run_signals.py` gets a new `--strategy <name>` flag (overrides router for ad-hoc runs); if omitted, uses router

**Files to modify:**
- `scheduler.py` — replace direct `run_signals_batch(...)` call with router-based loop
- `run_signals.py` — accept `--strategy` arg; if omitted, look up per-ticker via router

**Files to create:**
- `src/router.py`
- `config/strategies.toml`
- `tests/unit/test_router.py`

**Test plan:**
- `test_router.py` — assert ticker→strategy resolution + default fallback
- Update `tests/unit/test_scheduler.py` — assert multi-strategy dispatch (mock 2 strategies, assert both invoked)

**Acceptance:**
- `python scheduler.py --run-now` invokes WheelStrategy for AAPL/NVDA AND PutCreditSpreadStrategy for SPY in the same job (after B1 lands)
- `python run_signals.py --tickers SPY --strategy put_credit_spread --adapter fixture` works as override

---

### Phase A6 — OrderIntent abstraction in executor

**Goal:** `OrderExecutor` accepts a generic `OrderIntent` (1 or more legs) and translates to Alpaca's API for single-leg equity, single-leg option, or multi-leg option combos.

**Prerequisites:** A1

**Contracts:**

```python
# src/order_executor.py
from typing import Protocol
from pydantic import BaseModel
from src.signal_output import Leg

class OrderIntent(BaseModel):
    strategy_name: str
    ticker: str  # underlying symbol (for logging/audit)
    legs: list[Leg]
    notes: str = ""

class OrderResult(BaseModel):
    leg_index: int
    order_id: str
    status: str  # "accepted" | "filled" | "rejected" | "partial" | ...
    occ_symbol: str | None = None  # populated for option legs
    filled_price: float | None = None

class OrderExecutor(Protocol):
    def execute(self, intent: OrderIntent) -> list[OrderResult]: ...

class AlpacaOrderExecutor:
    def __init__(self, trading_client): ...

    def execute(self, intent: OrderIntent) -> list[OrderResult]:
        legs = intent.legs
        if not legs:
            raise ValueError("OrderIntent has no legs")
        if len(legs) == 1 and legs[0].asset_class == "equity":
            return [self._submit_equity(legs[0])]
        if len(legs) == 1 and legs[0].asset_class == "option":
            return [self._submit_single_option(legs[0])]
        if all(l.asset_class == "option" for l in legs) and 2 <= len(legs) <= 4:
            return self._submit_mleg(intent)
        raise ValueError(f"unsupported intent shape: {len(legs)} legs, classes {[l.asset_class for l in legs]}")

    def _submit_equity(self, leg: Leg) -> OrderResult: ...
    def _submit_single_option(self, leg: Leg) -> OrderResult: ...  # existing OCC path
    def _submit_mleg(self, intent: OrderIntent) -> list[OrderResult]: ...
```

**Alpaca multi-leg notes:**
- Use `alpaca.trading.requests.OptionLegRequest` and the `OrderClass.MLEG` on a `MarketOrderRequest`/`LimitOrderRequest`. Verify `alpaca-py >= 0.21` (older versions do not support mleg). If older, bump version in `pyproject.toml`.
- Net credit/debit for the combo is computed from leg `limit_price` × side sign; pass as the parent order's `limit_price`.
- All legs must share an underlying and an expiry for verticals/iron condors; calendar spreads use different expiries (Alpaca supports both).

**Deliverables:**
- Refactor `src/order_executor.py` — keep `build_occ_symbol` (already correct); replace `place_order_from_card` with `execute(intent)`
- The Wheel adapter for the executor: card.legs (1 leg, option) → OrderIntent → execute. No new code in WheelStrategy needed.
- Update `signal_engine.run_signal()` to build OrderIntent from card.legs (already shown in A2 sketch)

**Files to modify:**
- `src/order_executor.py` (large refactor)
- `src/signal_engine.py` (use `OrderIntent` + `executor.execute()`)

**Test plan:**
- Update `tests/unit/test_order_executor.py`:
  - Existing single-leg option test → reuse with OrderIntent wrapper
  - Add single-leg equity test (mock Alpaca's `submit_order` returning an equity order ID)
  - Add 2-leg option test (put credit spread shape) — assert `OrderClass.MLEG` + 2 OptionLegRequests passed to Alpaca
  - Add 4-leg option test (iron condor shape)

**Acceptance:**
- `executor.execute(intent)` returns `list[OrderResult]` with one entry per leg
- Wheel SELL_PUT still produces 1 OCC-format option order via the single-option code path (no regression)
- A 2-leg `OrderIntent` for SPY put credit spread submits a paper `mleg` order with 2 returned order IDs

---

### Phase A7 — Split backtester per-strategy

**Goal:** The current `src/backtester.py` is Wheel-only (BS pricing, phase A/B logic, assignment, EMA/RSI filter imports). Split it: the generic engine handles the date loop, walk-forward, equity curve, metrics; each strategy provides its own `simulate_trade()`.

**Prerequisites:** A1, A2

**Contracts:**

```python
# src/strategies/base.py — add to Protocol
from datetime import date
from typing import Any
import pandas as pd
from src.signal_output import SignalCard

class TradeResult(BaseModel):
    entry_date: date
    exit_date: date
    pnl: float  # realized P&L in dollars
    metadata: dict[str, Any] = {}  # strategy-specific (e.g., {"assignment": True, "premium": 7.86})

class Strategy(Protocol):
    ...

    def simulate_trade(
        self,
        card: SignalCard,
        future_ohlcv: pd.DataFrame,  # OHLCV bars after card.signal_timestamp
        future_options_chains: dict[date, pd.DataFrame] | None = None,
    ) -> TradeResult:
        """Given an emitted card and future market data, return the realized trade outcome.
        For Wheel: BS-price the short put, decide assignment vs expire vs early close.
        For PCS: max(net debit at expiry, max_loss); apply exit rules.
        For RSI2: bar-by-bar P&L until exit signal or max_hold_days.
        """
        ...
```

**Generic engine:**

```python
# src/backtester.py
class BacktestEngine:
    def __init__(self, strategy: Strategy, adapter: DataAdapter, initial_capital: float = 25000.0): ...

    def run(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> BacktestResult:
        """Walk-forward: for each day in [start, end]:
        1. Fetch trailing OHLCV via adapter
        2. Call strategy.emit_signal_card(ticker, adapter_window, state)
        3. If actionable, call strategy.simulate_trade(card, future_window, future_chains)
        4. Apply pnl to equity curve, log trade
        5. Update state per strategy's get_state_schema (e.g., Wheel goes A→B on assignment)
        """

class BacktestResult(BaseModel):
    equity_curve: list[tuple[date, float]]
    trades: list[TradeResult]
    metrics: dict[str, float]  # win_rate, total_pnl, sharpe, max_dd, ...
```

**Deliverables:**
- Add `simulate_trade` to the `Strategy` Protocol and `TradeResult` to `src/strategies/base.py`
- Extract Wheel-specific logic from `src/backtester.py` → `src/strategies/wheel/backtester.py` (BS pricing, delta-strike search, assignment logic, phase A/B sequencing)
- Generic `BacktestEngine` knows nothing about options; calls `strategy.simulate_trade()`

**Files to modify:**
- `src/backtester.py` (gut to ~200 lines of generic loop)
- `src/strategies/wheel/strategy.py` (implement `simulate_trade` — moved from backtester)
- `streamlit_app.py` Backtest page (pass strategy to engine, not hardcoded WheelStrategy)

**Files to create:**
- `src/strategies/wheel/backtester.py` (BS pricing helper module; called by `WheelStrategy.simulate_trade`)
- `tests/unit/test_backtester_generic.py` (engine in isolation, with a mock strategy)

**Test plan:**
- Update `tests/unit/test_backtester.py` — Wheel-specific tests move to `tests/unit/test_wheel_backtester.py`
- New: `tests/unit/test_backtester_generic.py` — feed mock Strategy that always emits same card; assert engine loops + computes equity correctly

**Acceptance:**
- Wheel backtest on the existing fixture/date range produces identical numbers (within rounding) to pre-refactor
- A mock equity-only strategy can be backtested through the same engine without code changes

---

### Phase A8 — Dashboard strategy-awareness

**Goal:** Strip Wheel hardcodes from `streamlit_app.py` so pages render whatever strategies are configured.

**Prerequisites:** A1, A2, A3, A5

**Deliverables:**
- Remove module constants `STATE_PATH = "wheel_state.json"` and `CONFIG_PATH = "config/wheel.toml"`
- Sidebar gets a **Strategy** selector (default: "all"); pages filter by selected strategy
- Signal cards in the grid render `signal_type` as the badge (not hardcoded SELL_PUT/SELL_CALL)
- **Ticker Detail** page reads strategy assignment from router; Wheel-specific phase widgets only show when ticker's strategy = "wheel"
- **Run Signals** page: pick strategy first, then tickers (filtered to that strategy's universe / fixture availability)
- **Backtest** page: pick strategy + ticker
- **Configuration** page: section per strategy (Wheel params, PCS params, RSI2 params), plus Router page to edit assignments

**Files to modify:**
- `streamlit_app.py` (large; consider extracting per-page modules to `dashboard/pages/`)
- `src/dashboard_data.py` (strategy-aware queries: `get_signals(strategy_name=None)`, `get_tracked_tickers(strategy_name=None)`)

**Test plan:**
- Update `tests/unit/test_dashboard_data.py` — assert filtering by `strategy_name`
- Manual: load dashboard with both wheel and put_credit_spread tickers in state; switch sidebar filter; verify correct subset renders

**Acceptance:**
- `grep -E "(STATE_PATH = .wheel_state|CONFIG_PATH = .wheel\.toml|Phase A|Phase B)" streamlit_app.py` returns 0 hits outside the explicitly Wheel-gated Ticker Detail section
- Dashboard renders signals from multiple strategies in the same grid, each with its own `signal_type` badge

---

## Track B — First new strategies (after Track A complete)

**Why these two:** Put Credit Spreads is the closest cousin to the Wheel — same VRP edge, same indicators, same DTE window — but capital-defined and 2-legged, which forces use of every Track A abstraction (`OrderIntent` multi-leg, `payload` for spread-specific fields, multi-leg backtester). RSI(2) on an ETF basket is the cleanest non-correlated equity strategy: small, well-documented, and tests the `DataAdapter` (non-options) path + `get_multi_ohlcv` for basket dispatch.

After these two land, adding **Iron Condors** (4-leg, same VRP) and **Long Earnings Straddles** (after Phase 11 calendar feed) becomes near-trivial — each is one new strategy module + config file, no further core changes.

### Phase summary

| # | Name | Effort | Prerequisites |
|---|---|---|---|
| B1 | Put Credit Spread strategy | Medium-Large | Track A complete |
| B2 | RSI(2) ETF basket strategy | Medium | Track A complete |

---

### Phase B1 — Put Credit Spread strategy

**Goal:** Defined-risk SPY/QQQ/IWM put credit spreads. Sell short put at ~0.30 delta, buy long put 5–10 strikes lower (or ~0.10 delta), 30–45 DTE, close at 50% max profit or 21 DTE.

**Prerequisites:** Track A complete (especially A1 multi-leg SignalCard, A6 OrderIntent multi-leg)

**Strategy logic:**

1. **Trend / momentum filters (reuse Wheel filters):** EMA50 trend up, RSI(14) in 35–65, BB %B not extreme
2. **Fetch options chain** for DTE 30–45
3. **Short-leg selection (`SpreadShortLegFilter`):** find put at ~0.30 delta (tolerance ±0.05)
4. **Long-leg selection (`SpreadLongLegFilter`):** find put `wing_width_strikes` strikes below short, OR at ~0.10 delta — whichever the config sets
5. **Credit / width validation (`MinCreditFilter`):** reject spread if `(short_bid - long_ask) / (short_strike - long_strike) < min_credit_to_width_pct / 100` — protects against "too cheap" spreads in low-vol regimes
6. **Emit SignalCard with 2 legs:**
   - `legs[0]`: sell short put (positive credit contribution)
   - `legs[1]`: buy long put (debit cost — the wing)
   - `signal_type`: `"SELL_PUT_SPREAD"`
   - `payload`: `{"net_credit": x, "max_profit": x, "max_loss": width - x, "credit_to_width_pct": ..., "iv_rank": ..., "dte": ...}`

**Config:**

```toml
# config/put_credit_spread.toml
[put_credit_spread]
# Short-leg
short_delta_target = 0.30
short_delta_tolerance = 0.05

# Long-leg (choose ONE selection mode)
long_leg_mode = "width"  # "width" | "delta"
wing_width_strikes = 5        # used when long_leg_mode = "width"
long_delta_target = 0.10      # used when long_leg_mode = "delta"
long_delta_tolerance = 0.03

# DTE window
dte_min = 30
dte_max = 45

# Trend filters (same as Wheel)
ema_period = 50
rsi_min = 35
rsi_max = 65
bb_period = 20
bb_std_dev = 2.0

# Trade quality
min_credit_to_width_pct = 25  # reject if credit/width < 25%

# Exit rules (consumed by Phase 9 exit_engine once it lands)
profit_target_pct = 50
dte_exit = 21
stop_loss_multiple = 2.0

[universe]
tickers = ["SPY", "QQQ", "IWM"]
```

**State schema:**

```python
# src/strategies/spreads/state.py
class PCSState(BaseModel):
    open_spread: bool = False
    short_strike: float | None = None
    long_strike: float | None = None
    expiry: str | None = None
    entry_credit: float | None = None
    entry_date: str | None = None
```

**Files to create:**
- `src/strategies/spreads/__init__.py`
- `src/strategies/spreads/put_credit_spread.py` — `PutCreditSpreadStrategy` class with `@register("put_credit_spread")`
- `src/strategies/spreads/filters.py` — `SpreadShortLegFilter`, `SpreadLongLegFilter`, `MinCreditFilter` (Phase Gate, EMA, RSI, BB reused from `wheel/filters.py` — extract those to `src/strategies/common/filters.py` if cleaner)
- `src/strategies/spreads/state.py`
- `src/strategies/spreads/backtester.py` — `simulate_trade`: at expiry → `pnl = entry_credit - max(short_strike - underlying_at_exit, 0) + max(long_strike - underlying_at_exit, 0)`, capped at `max_loss`; at 50% profit → close early
- `config/put_credit_spread.toml`
- `fixtures/SPY_options.json` — multi-strike chain spanning ~10 strikes around ATM at 30–45 DTE
- `fixtures/SPY_ohlcv.json` — 60 bars
- `tests/unit/test_put_credit_spread.py`
- `tests/unit/test_spread_filters.py`
- `tests/integration/test_pcs_scenarios.py` — end-to-end fixture run

**Optional refactor:** extract `EMATrendFilter`, `RSIFilter`, `BollingerFilter` to `src/strategies/common/filters.py` since both Wheel and PCS use them. Keep Wheel filters in `wheel/filters.py` for backward-compat imports.

**Acceptance:**
- `python run_signals.py --tickers SPY --strategy put_credit_spread --adapter fixture` emits a SignalCard with `signal_type="SELL_PUT_SPREAD"`, `len(legs) == 2`, `legs[0].side="sell"`, `legs[1].side="buy"`, `payload["max_profit"] > 0`
- `python run_signals.py --tickers SPY --strategy put_credit_spread --adapter alpaca --execute` submits a multi-leg paper order; response has 2 order IDs; both visible in Alpaca paper account orders list
- Backtest on 2-year SPY history (via streamlit_app Backtest page) produces win rate, total P&L, max DD; trade log shows entry credit + exit P&L per trade
- Dashboard renders PCS signals with both legs and `max_profit` / `max_loss` in the payload section

---

### Phase B2 — RSI(2) ETF basket strategy

**Goal:** Connors RSI(2) mean-reversion on a 10–15 ETF universe. Long-only equity, daily cron, 200-day MA trend filter.

**Prerequisites:** Track A complete (especially A4 `DataAdapter.get_multi_ohlcv`)

**Strategy logic:**

For each ticker in the universe (evaluated independently):

1. Fetch trailing 250 days of OHLCV
2. Compute RSI(2) and 200-day SMA
3. **If state.in_position:**
   - Exit if `RSI(2) > exit_rsi_min` OR `days_held >= max_hold_days`
   - Emit `signal_type="SELL_EQUITY"` with 1 leg: `sell` `state.shares` of the ticker
4. **Else (not in position):**
   - If `close > SMA(200)` AND `RSI(2) < entry_rsi_max`:
   - Position size: `floor(position_size_pct/100 × account_equity / current_close)` shares
   - Emit `signal_type="BUY_EQUITY"` with 1 leg: `buy` N shares
5. **Else:** `signal_type="NO_SIGNAL"`, reason "rsi_not_oversold" or "below_200dma"

`indicators` field carries `{"rsi2": ..., "sma200": ..., "close": ...}`; `payload` carries `{"days_held": ..., "entry_price": ...}` when in position.

**Config:**

```toml
# config/rsi2.toml
[rsi2]
rsi_period = 2
entry_rsi_max = 10
exit_rsi_min = 70
trend_filter_ma = 200
max_hold_days = 5
position_size_pct = 5  # % of account equity per signal

[universe]
tickers = ["SPY", "QQQ", "IWM", "EEM", "EFA", "TLT", "GLD", "XLE", "XLF", "XLK", "XLV", "XLP", "XLY", "XLU", "XLI"]
```

**State schema:**

```python
# src/strategies/momentum/state.py
class RSI2State(BaseModel):
    in_position: bool = False
    entry_date: str | None = None
    entry_price: float | None = None
    shares: int = 0
```

**Files to create:**
- `src/strategies/momentum/__init__.py`
- `src/strategies/momentum/rsi2.py` — `RSI2Strategy` with `@register("rsi2")`
- `src/strategies/momentum/state.py`
- `src/strategies/momentum/backtester.py` — `simulate_trade`: bar-by-bar; `pnl = (exit_price - entry_price) * shares`, costs subtracted (commission, half-spread slippage)
- `config/rsi2.toml`
- `fixtures/SPY_rsi2_ohlcv.json` — 300 bars spanning known RSI(2) < 10 instances for deterministic test
- `tests/unit/test_rsi2.py`
- `tests/integration/test_rsi2_scenarios.py`

**Indicator reuse:** RSI already lives in `src/indicator_engine.py` from the Wheel work. Use it directly. Add `sma(series, period)` if not present.

**Position sizing:** RSI2 needs account equity. Until Phase 10 (RiskManager) lands, hardcode `account_equity = config.get("paper_starting_equity", 25000.0)` and TODO-comment it. After Phase 10, source from `adapter.get_account()` (new method — add to `DataAdapter` if needed).

**Acceptance:**
- `python run_signals.py --tickers SPY --strategy rsi2 --adapter fixture` emits BUY_EQUITY when fixture RSI(2) < 10 AND close > 200-DMA
- Multi-ticker batch via scheduler runs RSI2 on the 15-ETF universe in one job (verify `adapter.get_multi_ohlcv` called once for the basket, not 15 times)
- Backtest on 5-year SPY history produces ~60–65% win rate, ~5–10 trades/year, Sharpe ~0.5–0.8
- Dashboard adds RSI2 to strategy selector; signal cards render correctly with equity leg (no strike/expiry fields)

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

> **Largely subsumed by Track A + B.** Once Track A (architecture refactor) and Phases B1 (PCS) + B2 (RSI2) are complete, the platform already runs multiple strategies in parallel via the router. What remains under this phase number is: (a) the **capital allocator** that splits portfolio equity across strategy buckets (e.g., 50% Wheel, 30% PCS, 20% RSI2) with rebalancing, and (b) **additional strategy modules** beyond B1/B2 — Iron Condor (4-leg, reuses B1 mleg path), Long Earnings Straddle (needs Phase 11 calendar feed), Calendar Spread (multi-expiry mleg). Each new strategy module is now a "drop in a class + config" exercise — no further core changes.

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

> **DO NOT START PHASE 16 UNTIL TRACK A IS COMPLETE.** Persisting Wheel-shaped JSON into SQLite before the schema is generalized means migrating the database twice — once to the new shape, and again whenever a new strategy adds a payload field. Track A first, then Phase 16 can model `(strategy_name, ticker)` as a composite key from day one.

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

## Suggested ordering rationale (REVISED)

The previous ordering (Phase 8 → 9 → 10 → ...) was written assuming a Wheel-only architecture. With Track A + B inserted, the new sequence is:

### Step 1 — Foundation (do these in order)

1. **Track A — Phases A1 → A8** (~1 focused week) — architecture refactor; everything below assumes this is done.
2. **Phase B1 — Put Credit Spreads** (Medium-Large) — first non-Wheel strategy; validates the refactor by exercising multi-leg orders.
3. **Phase B2 — RSI(2) ETF basket** (Medium) — first equity-only strategy; validates `DataAdapter` (non-options) path.

By this point: 3 live strategies, all using the same orchestrator, executor, backtester, dashboard, and state store.

### Step 2 — Operational maturity (path to live capital)

4. **Phase 8 — Position Management & Mark-to-Market** — prerequisite for any exit logic
5. **Phase 9 — Exit Rules & Position Lifecycle** — makes Wheel + PCS actually self-managing
6. **Phase 10 — Risk Management & Position Sizing** — required before any live capital touches it
7. **Phase 11 — Event & Calendar Awareness** — cheap insurance against earnings/FOMC blowups
8. **Phase 14 — Notifications & Alerting** — operational visibility before going live
9. **Phase 17 — Live Trading Safety Harness** — final gate before flipping paper → live

### Step 3 — Quality and reach (post-MVP)

10. **Phase 13 — Advanced Backtesting** — informs parameter choices once everything else is solid (Sharpe, Sortino, slippage model, walk-forward optimizer)
11. **Phase 12 — Enhanced AI Research** — quality-of-life upgrade; Claude as a research agent with tool use
12. **Phase 15 — Multi-Strategy Framework** — **most of this is already done by Track A.** What remains: a top-level capital allocator splitting equity across strategy buckets, plus additional strategy modules (Iron Condor, Long Earnings Straddle filtered by implied-vs-realized move, Calendar Spread).
13. **Phase 16 — Production Infrastructure** — SQLite + Docker + FastAPI + GitHub Actions cron. **Only after Track A is complete** (otherwise you bake Wheel-shaped schemas into the database).

### Why the inserted order

- **Track A first, before any feature work:** every feature phase (positions, exits, risk, notifications, safety harness) touches the orchestrator, SignalCard, executor, or state store. Doing them on the Wheel-only shape and then refactoring later means rewriting each of those features. Doing Track A first means those features are written once against the generic shape.
- **Track B before Phase 8 (positions):** Position tracking is far easier to design when you already have multiple strategy shapes to support; otherwise you'll build a single-leg-only `PositionStore` and refactor it the moment PCS lands.
- **Phase 15 deprioritized:** Track A subsumes the architectural half of Phase 15. The remaining work (new strategy modules + capital allocation across strategies) is small and can happen any time after Track B.

### Minimum viable income engine (paper-traded)

Track A → B1 → B2 → 8 → 9 → 10 → 11 → 17 = approx 4–6 weeks of focused work. After that, the system is genuinely safe to flip from paper to live with a small allocation.

---

## Out-of-scope / explicitly not doing

- **HFT or sub-second strategies** — Alpaca free tier and the daily-cron architecture aren't suited
- **Hosting client funds / multi-user** — personal-use system; SOC2 / fiduciary concerns are not in scope
- **Equity strategies beyond options income** — keep the system focused
- **Closed-source data feeds (Bloomberg, Refinitiv)** — relying on free tiers (Alpaca IEX, yfinance, NewsAPI free)
