#!/usr/bin/env python3
"""
Claude Trader — multi-strategy Streamlit dashboard.

Run:
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import tomli_w
from dotenv import load_dotenv

load_dotenv()

import src.strategies  # noqa: F401 — fire @register decorators
from src.dashboard_data import (
    filter_signals,
    get_all_ticker_states,
    get_iv_series,
    last_signal_by_ticker,
    load_all_signals,
    tracked_tickers,
)
from src.router import load_router, resolve_strategy
from src.state_store import get_strategy_state, get_ticker_state, set_ticker_state
from src.strategies.registry import list_strategies

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_PATH   = "wheel_state.json"
SIGNALS_PATH = "signals.jsonl"
IV_DIR       = "iv_history"
CONFIG_PATH  = "config/wheel.toml"   # default strategy config (Wheel)
ROUTER_PATH  = Path("config/strategies.toml")

PHASE_COLORS  = {"A": "#58a6ff", "B": "#bc8cff"}
SIGNAL_COLORS = {
    "SELL_PUT": "#3fb950", "SELL_CALL": "#bc8cff", "NO_SIGNAL": "#8b949e",
    "SELL_PUT_SPREAD": "#58a6ff", "BUY_EQUITY": "#79c0ff", "SELL_EQUITY": "#f85149",
}
TIER_COLORS   = {"HIGH": "#3fb950", "MEDIUM": "#d29922", "LOW": "#f85149"}
STRATEGY_COLORS = {"wheel": "#58a6ff", "put_credit_spread": "#bc8cff", "rsi2": "#3fb950"}

FIXTURES_DIR = Path("fixtures")

PLOTLY_CONFIG = {"displayModeBar": False}

# Strategies that never touch an options chain (fixture needs OHLCV only)
EQUITY_ONLY_STRATEGIES = {"rsi2"}

# ---------------------------------------------------------------------------
# Cached data layer — every reader is keyed on the source file's mtime, so a
# rerun costs ~0 until the CLI/scheduler actually writes new data.
# ---------------------------------------------------------------------------


def _mtime(path: str | Path) -> float:
    p = Path(path)
    return p.stat().st_mtime if p.exists() else 0.0


@st.cache_data(show_spinner=False)
def _signals_cached(mtime: float) -> list[dict]:
    return load_all_signals(SIGNALS_PATH)


@st.cache_data(show_spinner=False)
def _states_cached(mtime: float) -> dict[str, dict]:
    return get_all_ticker_states(STATE_PATH)


@st.cache_data(show_spinner=False)
def _iv_cached(ticker: str, mtime: float) -> list[dict]:
    return get_iv_series(ticker, IV_DIR)


@st.cache_data(show_spinner=False)
def _router_cached(mtime: float) -> dict:
    try:
        return load_router(ROUTER_PATH)
    except FileNotFoundError:
        return {}


def signals_data() -> list[dict]:
    return _signals_cached(_mtime(SIGNALS_PATH))


def states_data() -> dict[str, dict]:
    return _states_cached(_mtime(STATE_PATH))


def iv_data(ticker: str) -> list[dict]:
    return _iv_cached(ticker, _mtime(Path(IV_DIR) / f"{ticker.upper()}.jsonl"))


def _router_cfg() -> dict:
    return _router_cached(_mtime(ROUTER_PATH))


@st.cache_resource(show_spinner=False)
def _get_adapter(name: str):
    """Adapters are stateless clients — build once per server process."""
    if name == "fixture":
        from src.adapters.fixture_adapter import FixtureAdapter
        return FixtureAdapter()
    from src.adapters.alpaca_adapter import AlpacaPaperAdapter
    return AlpacaPaperAdapter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixture_tickers(require_options: bool = True) -> list[str]:
    """Return tickers with fixture files. Equity-only strategies (e.g. rsi2)
    don't need an options fixture, so pass require_options=False for those."""
    if not FIXTURES_DIR.exists():
        return []
    ohlcv   = {p.stem.replace("_ohlcv", "") for p in FIXTURES_DIR.glob("*_ohlcv.json")}
    if not require_options:
        return sorted(ohlcv)
    options = {p.stem.replace("_options", "") for p in FIXTURES_DIR.glob("*_options.json")}
    return sorted(ohlcv & options)


def _ticker_strategy(ticker: str, router_cfg: dict | None = None) -> str:
    cfg = router_cfg if router_cfg is not None else _router_cfg()
    if not cfg:
        return "wheel"
    return resolve_strategy(ticker, cfg)


def _save_router_cfg(cfg: dict) -> None:
    with open(ROUTER_PATH, "wb") as f:
        tomli_w.dump(cfg, f)


def _alpaca_available() -> bool:
    import os
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="background:rgba({_hex_to_rgb(color)},.15);color:{color};'
        f'padding:2px 10px;border-radius:12px;font-size:12px;font-weight:700;">'
        f'{text}</span>'
    )


def _hex_to_rgb(h: str) -> str:
    h = h.lstrip("#")
    return ",".join(str(int(h[i:i+2], 16)) for i in (0, 2, 4))


def _load_toml(path: str | Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _save_toml(path: str | Path, cfg: dict) -> None:
    with open(path, "wb") as f:
        tomli_w.dump(cfg, f)


def _strategy_badge(strat: str) -> str:
    return _badge(strat.replace("_", " "), STRATEGY_COLORS.get(strat, "#8b949e"))


# ---------------------------------------------------------------------------
# Page: Dashboard
# ---------------------------------------------------------------------------


def _dashboard_grid(strat_filter: str) -> None:
    states     = states_data()
    records    = signals_data()
    tickers    = tracked_tickers(states, records)
    last_sigs  = last_signal_by_ticker(records)
    router_cfg = _router_cfg()

    if strat_filter != "All":
        tickers = [
            t for t in tickers
            if ((last_sigs.get(t) or {}).get("strategy_name")
                or _ticker_strategy(t, router_cfg)) == strat_filter
        ]
        if not tickers:
            st.info(f"No tickers currently on **{strat_filter}**.")
            return

    cols = st.columns(3)
    for i, ticker in enumerate(tickers):
        state = states.get(ticker, {})
        sig   = last_sigs.get(ticker)
        phase = state.get("phase", "A")
        strat = (sig or {}).get("strategy_name") or _ticker_strategy(ticker, router_cfg)

        with cols[i % 3]:
            phase_badge  = (
                _badge(f"Phase {phase}", PHASE_COLORS.get(phase, "#8b949e"))
                if strat == "wheel" else ""
            )
            sig_phase    = sig.get("signal_type", "—") if sig else "—"
            sig_color    = SIGNAL_COLORS.get(sig_phase, "#8b949e")
            signal_badge = _badge(sig_phase.replace("_", " "), sig_color) if sig else ""

            st.markdown(
                f"### {ticker} &nbsp; {_strategy_badge(strat)} &nbsp; {phase_badge} &nbsp; {signal_badge}",
                unsafe_allow_html=True,
            )

            if sig:
                c1, c2 = st.columns(2)
                _legs = sig.get("legs") or []
                _leg0 = _legs[0] if _legs else {}
                _payload = sig.get("payload") or {}
                c1.metric("Price",      f"${sig.get('underlying_price', 0):.2f}")
                _mid = _leg0.get("limit_price")
                _is_equity = _leg0.get("asset_class") == "equity"
                c1.metric("Limit" if _is_equity else "Option Mid", f"${_mid:.2f}" if _mid else "—")
                if _is_equity:
                    c2.metric("Shares", _leg0.get("qty", 0))
                else:
                    iv = _payload.get("iv_rank")
                    c2.metric("IV Rank", f"{iv:.1f}%" if iv is not None else "—")
                tier = sig.get("confidence_tier")
                if tier:
                    c2.markdown(_badge(tier, TIER_COLORS.get(tier, "#8b949e")), unsafe_allow_html=True)
                if _leg0.get("strike"):
                    st.caption(f"Strike ${_leg0['strike']:.2f}  ·  Expiry {_leg0.get('expiry','—')}  ·  DTE {_payload.get('dte','—')}")
                elif _is_equity and (sig.get("indicators") or {}).get("rsi2") is not None:
                    st.caption(f"RSI(2) {sig['indicators']['rsi2']:.1f}  ·  SMA200 ${sig['indicators'].get('sma200') or 0:.2f}")
                _order_ids = sig.get("order_ids") or []
                _order_sts = sig.get("order_statuses") or []
                if _order_ids:
                    st.caption(f"Order: {_order_ids[0][:12]}…  [{_order_sts[0] if _order_sts else '—'}]")
            else:
                st.caption("No signals yet")

            if state.get("cost_basis"):
                st.caption(f"Cost basis ${state['cost_basis']:.2f}  ·  {state.get('assignment_date','')}")

            if st.button("View Details", key=f"detail_{ticker}", use_container_width=True):
                st.session_state["_detail_ticker"] = ticker
                st.switch_page(PAGE_TICKER_DETAIL)
            st.divider()


def page_dashboard() -> None:
    st.title("Dashboard")

    tickers = tracked_tickers(states_data(), signals_data())
    if not tickers:
        st.info("No tickers tracked yet. Use **Configuration → Tickers** to add one.")
        return

    top_l, top_r = st.columns([3, 1])
    strat_filter = top_l.segmented_control(
        "Strategy filter",
        ["All"] + sorted(list_strategies()),
        default="All",
        label_visibility="collapsed",
    ) or "All"
    auto_refresh = top_r.toggle("Auto-refresh 30s", value=False, help="Refresh the grid every 30s")

    grid = st.fragment(_dashboard_grid, run_every=30 if auto_refresh else None)
    grid(strat_filter)


# ---------------------------------------------------------------------------
# Page: Ticker Detail
# ---------------------------------------------------------------------------


@st.fragment
def _phase_transition_fragment(ticker: str, phase: str) -> None:
    st.subheader("Phase Transition")
    if phase == "A":
        with st.form("assign_form"):
            cost = st.number_input("Cost basis (assignment price per share)", min_value=0.01, step=0.01)
            if st.form_submit_button("Record Assignment  (A → B)", type="primary"):
                set_ticker_state(STATE_PATH, ticker, {
                    "phase": "B", "shares_held": 100,
                    "cost_basis": round(cost, 4),
                    "assignment_date": date.today().isoformat(),
                    "open_put_strike": None, "open_put_expiry": None,
                })
                st.toast(f"{ticker} moved to Phase B @ ${cost:.2f}", icon="✅")
                st.rerun(scope="app")  # stats row lives outside this fragment
    else:
        if st.button("Record Exit  (B → A)", type="secondary"):
            set_ticker_state(STATE_PATH, ticker, {
                "phase": "A", "shares_held": 0, "cost_basis": None,
                "assignment_date": None, "open_put_strike": None, "open_put_expiry": None,
            })
            st.toast(f"{ticker} moved back to Phase A", icon="✅")
            st.rerun(scope="app")


def page_ticker_detail() -> None:
    tickers = tracked_tickers(states_data(), signals_data())
    if not tickers:
        st.info("No tickers tracked yet.")
        return

    preselect = st.session_state.pop("_detail_ticker", None)
    default_idx = tickers.index(preselect) if preselect in tickers else 0

    ticker = st.selectbox("Ticker", tickers, index=default_idx)
    strat  = _ticker_strategy(ticker)
    st.markdown(f"# {ticker} &nbsp; {_strategy_badge(strat)}", unsafe_allow_html=True)

    state      = get_ticker_state(STATE_PATH, ticker)
    signals    = filter_signals(signals_data(), ticker=ticker, n=50)
    last_sig   = signals[0] if signals else None
    iv_series  = iv_data(ticker)

    # --- Stats row ---
    phase = state.get("phase", "A")
    c1, c2, c3, c4, c5 = st.columns(5)
    if strat == "wheel":
        c1.metric("Phase",       phase)
        c2.metric("Shares Held", state.get("shares_held", 0))
        if state.get("cost_basis"):
            c3.metric("Cost Basis", f"${state['cost_basis']:.2f}")
    else:
        strat_state = get_strategy_state(strat, ticker)
        if strat == "rsi2":
            c1.metric("In Position", "Yes" if strat_state.get("in_position") else "No")
            c2.metric("Shares", strat_state.get("shares", 0))
            if strat_state.get("entry_price"):
                c3.metric("Entry Price", f"${strat_state['entry_price']:.2f}")
        elif strat == "put_credit_spread":
            c1.metric("Open Spread", "Yes" if strat_state.get("open_spread") else "No")
            if strat_state.get("short_strike"):
                c2.metric("Short Strike", f"${strat_state['short_strike']:.2f}")
            if strat_state.get("entry_credit"):
                c3.metric("Entry Credit", f"${strat_state['entry_credit']:.2f}")
    if last_sig:
        c4.metric("Last Price", f"${last_sig.get('underlying_price', 0):.2f}")
        iv = (last_sig.get("payload") or {}).get("iv_rank")
        c5.metric("IV Rank", f"{iv:.1f}%" if iv is not None else "—")

    # --- IV History chart (options strategies only — equity strategies have no IV) ---
    if strat in EQUITY_ONLY_STRATEGIES:
        ind = (last_sig or {}).get("indicators") or {}
        if ind.get("rsi2") is not None:
            st.caption(f"Latest indicators: RSI(2) {ind['rsi2']:.1f} · "
                       f"SMA200 ${ind.get('sma200') or 0:.2f} · close ${ind.get('close') or 0:.2f}")
        iv_series = []
    elif iv_series:
        st.subheader("IV History")
        df_iv = pd.DataFrame(iv_series)
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_iv["date"], y=df_iv["iv"],
            mode="lines+markers",
            line=dict(color="#58a6ff", width=2),
            marker=dict(size=5),
            fill="tozeroy",
            fillcolor="rgba(88,166,255,0.08)",
            hovertemplate="Date: %{x}<br>IV: %{y:.1%}<extra></extra>",
        ))
        fig.update_layout(
            height=250, margin=dict(l=0, r=0, t=10, b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(gridcolor="#21262d", color="#8b949e"),
            yaxis=dict(gridcolor="#21262d", color="#8b949e",
                       tickformat=".0%"),
        )
        st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)
    else:
        st.info("No IV history yet — run a signal scan first.")

    # --- Signal history table ---
    st.subheader("Signal History")
    if signals:
        rows = []
        for s in signals:
            _legs = s.get("legs") or []
            _leg0 = _legs[0] if _legs else {}
            _payload = s.get("payload") or {}
            _order_ids = s.get("order_ids") or []
            rows.append({
                "Time":       s.get("signal_timestamp", "")[:16].replace("T", " "),
                "Strategy":   s.get("strategy_name", "—"),
                "Signal":     s.get("signal_type", ""),
                "Strike":     _leg0.get("strike"),
                "Expiry":     _leg0.get("expiry") or "—",
                "Delta":      _leg0.get("delta_estimate"),
                "IV Rank":    _payload.get("iv_rank"),
                "Mid":        _leg0.get("limit_price"),
                "Confidence": s.get("confidence_tier") or "—",
                "Order":      (_order_ids[0][:8] if _order_ids else "") or "—",
            })
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            height=min(38 + 35 * len(rows), 420),
            column_config={
                "Strike":  st.column_config.NumberColumn(format="$%.2f"),
                "Mid":     st.column_config.NumberColumn(format="$%.2f"),
                "Delta":   st.column_config.NumberColumn(format="%.3f"),
                "IV Rank": st.column_config.ProgressColumn(
                    format="%.1f%%", min_value=0, max_value=100),
            },
        )
    else:
        st.info(f"No signals recorded for {ticker}.")

    # --- Phase transition (Wheel-only concept) ---
    if strat != "wheel":
        return
    _phase_transition_fragment(ticker, phase)


# ---------------------------------------------------------------------------
# Page: Run Signals
# ---------------------------------------------------------------------------


@st.fragment
def _run_signals_body() -> None:
    tracked    = tracked_tickers(states_data(), signals_data())
    router_cfg = _router_cfg()

    strategy_options = (["auto (router)"] if router_cfg else []) + sorted(list_strategies())
    col_s, col_a = st.columns(2)
    strategy_choice = col_s.selectbox(
        "Strategy", strategy_options, key="rs_strategy",
        help="auto resolves each ticker via config/strategies.toml; "
             "picking a name forces that strategy for all selected tickers.",
    )
    strategy_name = "auto" if strategy_choice.startswith("auto") else strategy_choice

    adapters = (["alpaca"] if _alpaca_available() else []) + ["fixture"]
    adapter = col_a.radio("Adapter", adapters, horizontal=True, key="rs_adapter")

    if adapter == "fixture":
        require_options = strategy_name not in EQUITY_ONLY_STRATEGIES and strategy_name != "auto"
        fix_tickers = _fixture_tickers(require_options=require_options)
        pool = [t for t in tracked if t in fix_tickers] or fix_tickers
        if not pool:
            st.warning("No fixture files found in fixtures/.")
        missing = [t for t in tracked if t not in fix_tickers]
        if missing:
            st.caption(f"No fixture for: {', '.join(missing)} — add fixture files or use Alpaca.")
        if strategy_name == "auto":
            st.caption("auto: tickers routed to an options strategy still need an options fixture.")
    else:
        pool = tracked
    if not _alpaca_available():
        st.caption("Alpaca hidden — set ALPACA_API_KEY + ALPACA_SECRET_KEY in .env to enable.")

    with st.form("run_form"):
        tickers  = st.multiselect("Tickers", options=pool, default=[t for t in pool[:5] if t in pool])
        col1, col2 = st.columns(2)
        execute  = col1.checkbox("Place paper orders  (--execute)", value=False)
        thesis   = col2.checkbox("Generate AI thesis  (--thesis)", value=False)
        run      = st.form_submit_button("Run Signal Scan", type="primary")

    if run:
        if not tickers:
            st.warning("Select at least one ticker.")
            return

        with st.spinner(f"Scanning {', '.join(tickers)} via {adapter}…"):
            try:
                from src.signal_engine import run_signals_batch

                data_adapter = _get_adapter(adapter)
                executor = None
                if execute:
                    from src.order_executor import AlpacaOrderExecutor
                    executor = AlpacaOrderExecutor()

                cards = run_signals_batch(
                    tickers=tickers,
                    adapter=data_adapter,
                    config_path=CONFIG_PATH,
                    state_path=STATE_PATH,
                    signals_path=SIGNALS_PATH,
                    print_output=False,
                    generate_thesis=thesis,
                    execute=execute,
                    executor=executor,
                    strategy_name=strategy_name if router_cfg else None,
                )

                for card in cards:
                    color = SIGNAL_COLORS.get(card.signal_type, "#8b949e")
                    tier_color = TIER_COLORS.get(card.confidence_tier or "", "#8b949e")
                    strat_badge = _strategy_badge(card.strategy_name)
                    if card.signal_type == "NO_SIGNAL":
                        st.markdown(strat_badge, unsafe_allow_html=True)
                        st.error(f"**{card.ticker}** — NO SIGNAL: {card.no_signal_reason}")
                    else:
                        leg0 = card.legs[0] if card.legs else None
                        is_equity = leg0 is not None and leg0.asset_class == "equity"
                        cols = st.columns([2, 1, 1, 1, 1, 2])
                        cols[0].markdown(f"{strat_badge} {_badge(card.signal_type.replace('_', ' '), color)}", unsafe_allow_html=True)
                        cols[0].markdown(f"**{card.ticker}**")
                        if is_equity:
                            cols[1].metric("Shares", leg0.qty)
                            cols[2].metric("Limit",  f"${leg0.limit_price:.2f}" if leg0.limit_price else "—")
                            rsi2_val = card.indicators.get("rsi2")
                            cols[3].metric("RSI(2)", f"{rsi2_val:.1f}" if rsi2_val is not None else "—")
                        else:
                            cols[1].metric("Strike",  f"${leg0.strike:.2f}" if leg0 and leg0.strike else "—")
                            cols[2].metric("Mid",     f"${leg0.limit_price:.2f}" if leg0 and leg0.limit_price else "—")
                            iv_rank = card.payload.get("iv_rank")
                            cols[3].metric("IV Rank", f"{iv_rank:.1f}%" if iv_rank is not None else "—")
                        cols[4].markdown(_badge(card.confidence_tier or "—", tier_color), unsafe_allow_html=True)
                        if card.order_ids:
                            cols[5].success(f"Order {card.order_ids[0][:8]}… [{card.order_statuses[0] if card.order_statuses else '—'}]")
                        if card.thesis_text:
                            st.caption(f"Thesis: {card.thesis_text}")
                        st.divider()
            except Exception as exc:
                st.error(f"Scan failed: {exc}")


def page_run_signals() -> None:
    st.title("Run Signals")
    st.caption("Scan tickers through each strategy's filter pipeline and optionally place paper orders.")
    _run_signals_body()


# ---------------------------------------------------------------------------
# Page: Positions (Phase 8) + exit-rule preview (Phase 9)
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _get_executor():
    from src.order_executor import AlpacaOrderExecutor
    return AlpacaOrderExecutor()


def _positions_body() -> None:
    from src.exit_engine import evaluate_position, load_exit_rules
    from src.positions import fetch_positions, load_position_events

    executor = _get_executor()
    try:
        positions = fetch_positions(executor, _router_cfg() or None)
    except Exception as exc:
        st.error(f"Could not fetch positions: {exc}")
        return

    # --- Account panel ---
    try:
        acct = executor.get_account()
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Account Equity", f"${acct['equity']:,.0f}" if acct.get("equity") else "—")
        a2.metric("Cash",           f"${acct['cash']:,.0f}" if acct.get("cash") else "—")
        a3.metric("Buying Power",   f"${acct['buying_power']:,.0f}" if acct.get("buying_power") else "—")
        obp = acct.get("options_buying_power")
        a4.metric("Options BP",     f"${obp:,.0f}" if obp else "—")
        st.divider()
    except Exception as exc:
        st.caption(f"Account snapshot unavailable: {exc}")

    rules = load_exit_rules(ROUTER_PATH if ROUTER_PATH.exists() else CONFIG_PATH)
    decisions = {p.symbol: evaluate_position(p, rules) for p in positions}

    total_upl = sum(p.unrealized_pnl or 0.0 for p in positions)
    total_mv  = sum(p.market_value or 0.0 for p in positions)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Open Positions",  len(positions))
    m2.metric("Market Value",    f"${total_mv:,.2f}")
    m3.metric("Unrealized P&L",  f"${total_upl:,.2f}")
    firing = [d for d in decisions.values() if d is not None]
    m4.metric("Exit Rules Firing", len(firing))

    if not positions:
        st.info("No open positions in the paper account. Place orders from **Run Signals** "
                "with *Place paper orders* checked.")
    else:
        rows = []
        for p in positions:
            d = decisions.get(p.symbol)
            rows.append({
                "Symbol":       p.symbol,
                "Strategy":     p.strategy_name or "—",
                "Type":         f"{p.side} {p.option_type or p.asset_class}",
                "Qty":          p.qty,
                "Entry":        p.avg_entry_price,
                "Mark":         p.current_price,
                "Unreal. P&L":  p.unrealized_pnl,
                "DTE":          p.dte,
                "% Max Profit": p.pct_max_profit,
                "Exit Rule":    f"⚠ {d.reason}" if d else "—",
            })
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Entry":        st.column_config.NumberColumn(format="$%.2f"),
                "Mark":         st.column_config.NumberColumn(format="$%.2f"),
                "Unreal. P&L":  st.column_config.NumberColumn(format="$%.2f"),
                "% Max Profit": st.column_config.ProgressColumn(
                    format="%.1f%%", min_value=-100, max_value=100),
            },
        )
        if firing:
            for d in firing:
                st.warning(f"**{d.symbol}** — {d.reason}: {d.detail} "
                           f"(the scheduler will close this on its next run)")

        # --- Manual close ---
        st.subheader("Close a Position")
        with st.form("close_position_form"):
            c1, c2 = st.columns([3, 1])
            sym = c1.selectbox("Position", [p.symbol for p in positions])
            if c2.form_submit_button("Close", type="primary"):
                from src.positions import append_position_event
                try:
                    result = executor.close_position(sym)
                    append_position_event({
                        "event": "manual_close_submitted",
                        "symbol": sym,
                        "reason": "manual",
                        "order_id": result.get("order_id"),
                        "status": result.get("status"),
                    })
                    st.toast(f"Close submitted for {sym} [{result.get('status')}]", icon="✅")
                    st.rerun(scope="fragment")
                except Exception as exc:
                    st.error(f"Close failed: {exc}")

    # --- State reconciliation ---
    st.subheader("Strategy State Sync")
    st.caption("Aligns state files (wheel phases, rsi2 in-position, PCS open-spread) "
               "with live positions. The scheduler does this automatically before each scan.")
    if st.button("Sync strategy state from positions"):
        from src.reconcile import sync_state_from_positions
        try:
            changes = sync_state_from_positions(positions, _router_cfg() or {})
            if changes:
                for c in changes:
                    st.toast(f"Synced {c['ticker']} ({c['strategy']})", icon="🔄")
                st.rerun(scope="app")  # state shown on other pages changed
            else:
                st.toast("All strategy state already in sync", icon="✅")
        except Exception as exc:
            st.error(f"Sync failed: {exc}")

    # --- Performance (realized P&L from fill ledger) ---
    st.subheader("Performance — Realized P&L")
    try:
        from src.pnl_ledger import realized_pnl_summary
        realized = realized_pnl_summary(executor, _router_cfg() or None)
        p1, p2 = st.columns([1, 3])
        p1.metric("Total Realized", f"${realized['total']:,.2f}")
        if realized["by_strategy"]:
            with p2:
                sc = st.columns(max(len(realized["by_strategy"]), 1))
                for i, (strat, pnl) in enumerate(sorted(realized["by_strategy"].items())):
                    sc[i].metric(strat, f"${pnl:,.2f}")
        closed_rows = [r for r in realized["by_symbol"] if r["closed_qty"] > 0]
        if closed_rows:
            st.dataframe(
                pd.DataFrame(closed_rows),
                use_container_width=True, hide_index=True,
                column_config={
                    "realized_pnl": st.column_config.NumberColumn("Realized P&L", format="$%.2f"),
                    "closed_qty":   st.column_config.NumberColumn("Closed Qty"),
                    "open_qty":     st.column_config.NumberColumn("Still Open"),
                },
            )
        else:
            st.caption("No closed round-trips yet — realized P&L appears after the first "
                       "position is opened and closed.")
    except Exception as exc:
        st.caption(f"Fill ledger unavailable: {exc}")

    # --- Event log ---
    events = load_position_events(n=25)
    if events:
        st.subheader("Recent Position Events")
        ev_rows = [
            {
                "Time":    (e.get("ts") or "")[:16].replace("T", " "),
                "Event":   e.get("event", "—"),
                "Symbol":  e.get("symbol", "—"),
                "Reason":  e.get("reason", "—"),
                "Detail":  e.get("detail", ""),
                "Status":  e.get("status", "—"),
            }
            for e in events
        ]
        st.dataframe(pd.DataFrame(ev_rows), use_container_width=True, hide_index=True)


def page_positions() -> None:
    st.title("Positions")
    st.caption("Live paper-account positions, mark-to-market P&L, and exit-rule status.")

    if not _alpaca_available():
        st.info("Positions require Alpaca credentials — set ALPACA_API_KEY + "
                "ALPACA_SECRET_KEY in .env.")
        return

    auto_refresh = st.toggle("Auto-refresh 30s", value=False, key="pos_refresh")
    body = st.fragment(_positions_body, run_every=30 if auto_refresh else None)
    body()


# ---------------------------------------------------------------------------
# Page: Backtest
# ---------------------------------------------------------------------------


class _WindowAdapter:
    """Serves a fixed OHLCV window as get_ohlcv — used for rolling backtests
    of equity strategies whose emit_signal_card only needs price history."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_ohlcv(self, ticker: str, bars: int, timeframe: str = "1Day") -> pd.DataFrame:
        return self._df.iloc[-bars:]


def _backtest_rsi2(strategy, hist: pd.DataFrame, ticker: str) -> list:
    """Walk-forward: emit real RSI2 cards on rolling windows, simulate on future bars."""
    trades = []
    warmup = 210  # 200-day SMA + slack
    i = warmup
    while i < len(hist) - 1:
        window = hist.iloc[:i]
        card = strategy.emit_signal_card(ticker, _WindowAdapter(window), {})
        if card.signal_type == "BUY_EQUITY":
            card.signal_timestamp = window.index[-1].to_pydatetime()
            result = strategy.simulate_trade(card, hist.iloc[i:])
            trades.append(result)
            exit_ts = pd.Timestamp(result.exit_date, tz="UTC")
            i = max(int(hist.index.searchsorted(exit_ts, side="right")), i + 1)
        else:
            i += 1
    return trades


def _backtest_pcs(hist: pd.DataFrame, ticker: str, dte_target: int = 35) -> list:
    """Simplified PCS walk-forward: open a 5%/10%-OTM put spread whenever flat;
    entry credit is Black-Scholes-priced from realized vol (see simulate_pcs_trade)."""
    from src.strategies.spreads.backtester import simulate_pcs_trade

    trades = []
    warmup = 60
    i = warmup
    while i < len(hist) - 1:
        window = hist.iloc[:i]
        spot = float(window["close"].iloc[-1])
        result = simulate_pcs_trade(
            ticker=ticker,
            short_strike=round(spot * 0.95, 0),
            long_strike=round(spot * 0.90, 0),
            entry_date=window.index[-1].date(),
            dte_target=dte_target,
            net_credit=0.0,  # <=0 → BS-priced at entry
            entry_ohlcv=window.iloc[-30:],
            future_ohlcv=hist.iloc[i:],
        )
        if result.metadata.get("outcome") == "OPEN":
            break  # ran off the end of history
        trades.append(result)
        exit_ts = pd.Timestamp(result.exit_date, tz="UTC")
        i = max(int(hist.index.searchsorted(exit_ts, side="right")) + 1, i + 1)
    return trades


def _render_trade_results(trades: list) -> None:
    """Shared metrics + equity curve + trade table for TradeResult lists."""
    if not trades:
        st.info("No trades generated — try a wider date range or relaxed config.")
        return

    wins = [t for t in trades if t.pnl > 0]
    total_pnl = sum(t.pnl for t in trades)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Trades",    len(trades))
    m2.metric("Win rate",  f"{100 * len(wins) / len(trades):.1f}%")
    m3.metric("Total P&L", f"${total_pnl:.2f}")
    m4.metric("Avg P&L",   f"${total_pnl / len(trades):.2f}")

    running, curve = 0.0, []
    for t in trades:
        running += t.pnl
        curve.append({"date": t.exit_date, "pnl": round(running, 2)})
    eq_df = pd.DataFrame(curve)
    fig = go.Figure(go.Scatter(x=eq_df["date"], y=eq_df["pnl"],
                               mode="lines+markers", name="Equity"))
    fig.update_layout(title="Cumulative P&L", xaxis_title="Exit date",
                      yaxis_title="P&L ($)", height=350)
    st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)

    rows = [
        {
            "Entry":   t.entry_date,
            "Exit":    t.exit_date,
            "Signal":  t.signal_type,
            "P&L":     t.pnl,
            "Outcome": t.metadata.get("outcome", "—"),
        }
        for t in trades
    ]
    st.dataframe(
        pd.DataFrame(rows), use_container_width=True, hide_index=True,
        column_config={"P&L": st.column_config.NumberColumn(format="$%.2f")},
    )


def page_backtest() -> None:
    st.title("Backtest")

    strategies = sorted(list_strategies())
    col0, col1, col2 = st.columns(3)
    bt_strategy = col0.selectbox("Strategy", strategies, key="bt_strategy",
                                 index=strategies.index("wheel") if "wheel" in strategies else 0)

    if bt_strategy == "wheel":
        st.caption("Walk-forward backtest using 30-day realized vol + Black-Scholes pricing.")
    elif bt_strategy == "put_credit_spread":
        st.caption("Simplified walk-forward: 5%/10%-OTM put spread whenever flat, "
                   "BS-priced entry credit from realized vol.")
    else:
        st.caption("Walk-forward RSI(2): real entry signals on rolling 200-day windows, "
                   "bar-by-bar exits (RSI recovery or max hold).")

    tracked     = tracked_tickers(states_data(), signals_data())
    fix_tickers = _fixture_tickers(require_options=False)
    adapters    = (["alpaca"] if _alpaca_available() else []) + ["fixture"]

    adapter = col1.radio("Adapter", adapters, horizontal=True, key="bt_adapter")
    if adapter == "fixture":
        pool = fix_tickers or ["WXYZ"]
    else:
        pool = tracked or ["AAPL"]
    ticker = col2.selectbox("Ticker", options=pool, key="bt_ticker")
    if not _alpaca_available():
        st.caption("Alpaca hidden — set ALPACA_API_KEY + ALPACA_SECRET_KEY in .env to enable.")

    with st.form("backtest_form"):
        col3, col4 = st.columns(2)
        start   = col3.date_input("Start date", value=date(2024, 1, 1))
        end     = col4.date_input("End date",   value=date(2024, 12, 31))
        phase   = None
        if bt_strategy == "wheel":
            phase = st.radio("Phase", ["A — Sell Put", "B — Sell Call"], horizontal=True)
        run     = st.form_submit_button("Run Backtest", type="primary")

    if not run:
        return

    with st.spinner(f"Running {bt_strategy} backtest for {ticker} ({start} → {end})…"):
        try:
            data_adapter = _get_adapter(adapter)

            if bt_strategy == "wheel":
                from src.backtester import BacktestEngine
                wheel_cfg = _load_toml(CONFIG_PATH).get("wheel", {})
                engine  = BacktestEngine(data_adapter, {"wheel": wheel_cfg})
                summary = engine.run(ticker, start, end, phase="A" if phase.startswith("A") else "B")

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Total trades",    summary.total_trades)
                m2.metric("Closed trades",   summary.closed_trades)
                m3.metric("Win rate",        f"{summary.win_rate:.1f}%")
                m4.metric("Total premium",   f"${summary.total_premium:.2f}")
                m5.metric("Total P&L",       f"${summary.total_pnl:.2f}")

                if summary.trades:
                    closed = [t for t in summary.trades if t.outcome != "OPEN"]
                    if closed:
                        cumulative, running = [], 0.0
                        for t in closed:
                            running += t.pnl
                            cumulative.append({"date": t.expiry_date, "pnl": round(running, 2)})
                        eq_df = pd.DataFrame(cumulative)
                        fig = go.Figure(go.Scatter(x=eq_df["date"], y=eq_df["pnl"],
                                                   mode="lines+markers", name="Equity"))
                        fig.update_layout(title="Cumulative P&L", xaxis_title="Expiry",
                                          yaxis_title="P&L ($/share)", height=350)
                        st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)

                    rows = [
                        {
                            "Entry":     t.entry_date,
                            "Expiry":    t.expiry_date,
                            "Strike":    t.strike,
                            "Premium":   t.estimated_premium,
                            "IV":        t.iv_at_entry,
                            "Outcome":   t.outcome,
                            "P&L":       t.pnl,
                        }
                        for t in summary.trades
                    ]
                    st.dataframe(
                        pd.DataFrame(rows), use_container_width=True, hide_index=True,
                        column_config={
                            "Strike":  st.column_config.NumberColumn(format="$%.2f"),
                            "Premium": st.column_config.NumberColumn(format="$%.2f"),
                            "P&L":     st.column_config.NumberColumn(format="$%.2f"),
                        },
                    )
                else:
                    st.info("No trades generated — try a wider date range or relaxed config.")

            else:
                hist = data_adapter.get_historical_ohlcv(ticker, start, end)
                if hist.empty:
                    st.info("No historical data for that range.")
                    return
                if bt_strategy == "rsi2":
                    if len(hist) < 220:
                        st.warning("RSI(2) needs ≥ 220 bars of history (200-day SMA warm-up) — "
                                   "widen the date range.")
                        return
                    from src.router import build_strategy
                    strategy = build_strategy("rsi2", _router_cfg())
                    trades = _backtest_rsi2(strategy, hist, ticker)
                else:  # put_credit_spread
                    trades = _backtest_pcs(hist, ticker)
                _render_trade_results(trades)

        except Exception as exc:
            st.error(f"Backtest failed: {exc}")


# ---------------------------------------------------------------------------
# Page: Configuration
# ---------------------------------------------------------------------------


@st.fragment
def _config_router_tab() -> None:
    st.subheader("Strategy Router")
    router_cfg = _router_cfg()
    if not router_cfg:
        st.info("No config/strategies.toml found — all tickers run the Wheel strategy.")
        return

    st.caption("Which strategy each ticker runs. Saved to `config/strategies.toml`. "
               "Note: saving rewrites the file and drops TOML comments.")
    assignments = router_cfg.get("router", {}).get("assignments", {})
    default_strat = router_cfg.get("router", {}).get("default_strategy", "wheel")
    st.markdown(f"Default strategy (unassigned tickers): **{default_strat}**")

    if assignments:
        df_router = pd.DataFrame(
            [{"Ticker": t, "Strategy": s} for t, s in sorted(assignments.items())]
        )
        st.dataframe(df_router, use_container_width=True, hide_index=True)

    known_strategies = sorted(list_strategies())
    col_assign, col_unassign = st.columns(2)
    with col_assign:
        st.markdown("**Assign / Reassign Ticker**")
        with st.form("router_assign_form"):
            r_ticker = st.text_input("Ticker").upper().strip()
            r_strat  = st.selectbox("Strategy", known_strategies)
            if st.form_submit_button("Save Assignment", type="primary"):
                if not r_ticker:
                    st.warning("Enter a ticker symbol.")
                else:
                    router_cfg.setdefault("router", {}).setdefault("assignments", {})[r_ticker] = r_strat
                    _save_router_cfg(router_cfg)
                    st.toast(f"{r_ticker} → {r_strat}", icon="✅")
                    st.rerun(scope="fragment")
    with col_unassign:
        st.markdown("**Remove Assignment**")
        with st.form("router_unassign_form"):
            r_remove = st.selectbox("Ticker", sorted(assignments.keys()) or ["—"])
            if st.form_submit_button("Remove", type="secondary"):
                if r_remove and r_remove != "—":
                    router_cfg.get("router", {}).get("assignments", {}).pop(r_remove, None)
                    _save_router_cfg(router_cfg)
                    st.toast(f"Removed {r_remove} (falls back to {default_strat})", icon="✅")
                    st.rerun(scope="fragment")


def _wheel_params_form() -> None:
    cfg   = _load_toml(CONFIG_PATH)
    wheel = cfg.get("wheel", {})
    st.caption("Changes are saved to `config/wheel.toml` immediately.")

    with st.form("strategy_form"):
        c1, c2 = st.columns(2)
        ema_period = c1.number_input(
            "EMA Period", min_value=5, max_value=200,
            value=int(wheel.get("ema_period", 50)), step=1,
            help="Lookback period for the 50-day EMA trend filter",
        )
        bb_period = c2.number_input(
            "Bollinger Band Period", min_value=5, max_value=100,
            value=int(wheel.get("bb_period", 20)), step=1,
        )
        bb_std_dev = c1.number_input(
            "Bollinger Band Std Dev", min_value=1.0, max_value=4.0,
            value=float(wheel.get("bb_std_dev", 2.0)), step=0.1, format="%.1f",
        )
        volume_lookback = c2.number_input(
            "Volume Lookback Bars", min_value=5, max_value=100,
            value=int(wheel.get("volume_lookback_bars", 20)), step=1,
        )

        st.divider()
        st.subheader("RSI Gate")
        r1, r2 = st.columns(2)
        rsi_min = r1.number_input("RSI Min", min_value=10, max_value=50,
                                   value=int(wheel.get("rsi_min", 35)), step=1)
        rsi_max = r2.number_input("RSI Max", min_value=50, max_value=90,
                                   value=int(wheel.get("rsi_max", 65)), step=1)

        st.subheader("Delta Target")
        d1, d2 = st.columns(2)
        delta_target    = d1.number_input("Delta Target",    min_value=0.10, max_value=0.50,
                                           value=float(wheel.get("delta_target", 0.30)),
                                           step=0.01, format="%.2f")
        delta_tolerance = d2.number_input("Delta Tolerance", min_value=0.01, max_value=0.15,
                                           value=float(wheel.get("delta_tolerance", 0.05)),
                                           step=0.01, format="%.2f")

        st.subheader("Days to Expiry Window")
        e1, e2 = st.columns(2)
        dte_min = e1.number_input("DTE Min", min_value=7,  max_value=60,
                                   value=int(wheel.get("dte_min", 30)), step=1)
        dte_max = e2.number_input("DTE Max", min_value=14, max_value=120,
                                   value=int(wheel.get("dte_max", 45)), step=1)

        if st.form_submit_button("Save Wheel Parameters", type="primary"):
            cfg["wheel"] = {
                "ema_period":                   ema_period,
                "rsi_min":                      rsi_min,
                "rsi_max":                      rsi_max,
                "delta_target":                 delta_target,
                "delta_tolerance":              delta_tolerance,
                "dte_min":                      dte_min,
                "dte_max":                      dte_max,
                "bb_period":                    bb_period,
                "bb_std_dev":                   bb_std_dev,
                "volume_lookback_bars":         volume_lookback,
                "soft_flag_confidence_penalty": wheel.get("soft_flag_confidence_penalty", "HIGH_TO_MEDIUM"),
            }
            _save_toml(CONFIG_PATH, cfg)
            st.toast("Wheel parameters saved to config/wheel.toml", icon="✅")


def _pcs_params_form() -> None:
    path = "config/put_credit_spread.toml"
    cfg  = _load_toml(path)
    pcs  = cfg.get("put_credit_spread", {})
    st.caption(f"Changes are saved to `{path}` immediately.")

    with st.form("pcs_form"):
        c1, c2 = st.columns(2)
        ema_period = c1.number_input("EMA Period", min_value=5, max_value=200,
                                     value=int(pcs.get("ema_period", 50)), step=1)
        min_iv_rank = c2.number_input("Min IV Rank (hard stop)", min_value=0.0, max_value=100.0,
                                      value=float(pcs.get("min_iv_rank", 25.0)), step=1.0, format="%.0f")

        st.subheader("RSI Gate")
        r1, r2 = st.columns(2)
        rsi_min = r1.number_input("RSI Min", min_value=10.0, max_value=50.0,
                                  value=float(pcs.get("rsi_min", 35.0)), step=1.0, format="%.0f")
        rsi_max = r2.number_input("RSI Max", min_value=50.0, max_value=90.0,
                                  value=float(pcs.get("rsi_max", 65.0)), step=1.0, format="%.0f")

        st.subheader("Spread Legs")
        d1, d2 = st.columns(2)
        short_delta = d1.number_input("Short Delta Target", min_value=0.10, max_value=0.50,
                                      value=float(pcs.get("short_delta_target", 0.30)),
                                      step=0.01, format="%.2f")
        short_tol   = d2.number_input("Short Delta Tolerance", min_value=0.01, max_value=0.15,
                                      value=float(pcs.get("short_delta_tolerance", 0.05)),
                                      step=0.01, format="%.2f")
        long_delta  = d1.number_input("Long Delta Target", min_value=0.05, max_value=0.30,
                                      value=float(pcs.get("long_delta_target", 0.15)),
                                      step=0.01, format="%.2f")
        long_tol    = d2.number_input("Long Delta Tolerance", min_value=0.01, max_value=0.15,
                                      value=float(pcs.get("long_delta_tolerance", 0.07)),
                                      step=0.01, format="%.2f")
        min_credit  = d1.number_input("Min Net Credit ($/share)", min_value=0.05, max_value=5.0,
                                      value=float(pcs.get("min_credit", 0.40)), step=0.05, format="%.2f")
        max_width   = d2.number_input("Max Spread Width ($)", min_value=1.0, max_value=50.0,
                                      value=float(pcs.get("max_spread_width", 15.0)), step=1.0, format="%.0f")

        st.subheader("Days to Expiry Window")
        e1, e2 = st.columns(2)
        dte_min = e1.number_input("DTE Min", min_value=7, max_value=60,
                                  value=int(pcs.get("dte_min", 30)), step=1)
        dte_max = e2.number_input("DTE Max", min_value=14, max_value=120,
                                  value=int(pcs.get("dte_max", 45)), step=1)

        if st.form_submit_button("Save PCS Parameters", type="primary"):
            cfg["put_credit_spread"] = {
                "ema_period":            ema_period,
                "rsi_min":               rsi_min,
                "rsi_max":               rsi_max,
                "dte_min":               dte_min,
                "dte_max":               dte_max,
                "short_delta_target":    short_delta,
                "short_delta_tolerance": short_tol,
                "long_delta_target":     long_delta,
                "long_delta_tolerance":  long_tol,
                "min_iv_rank":           min_iv_rank,
                "min_credit":            min_credit,
                "max_spread_width":      max_width,
            }
            _save_toml(path, cfg)
            st.toast(f"PCS parameters saved to {path}", icon="✅")


def _rsi2_params_form() -> None:
    path = "config/rsi2.toml"
    cfg  = _load_toml(path)
    r2c  = cfg.get("rsi2", {})
    st.caption(f"Changes are saved to `{path}` immediately.")

    with st.form("rsi2_form"):
        c1, c2 = st.columns(2)
        entry_max = c1.number_input("Entry RSI(2) Max (oversold)", min_value=1.0, max_value=30.0,
                                    value=float(r2c.get("entry_rsi_max", 10.0)), step=1.0, format="%.0f")
        exit_min  = c2.number_input("Exit RSI(2) Min (recovered)", min_value=50.0, max_value=95.0,
                                    value=float(r2c.get("exit_rsi_min", 70.0)), step=1.0, format="%.0f")
        trend_ma  = c1.number_input("Trend Filter SMA", min_value=50, max_value=250,
                                    value=int(r2c.get("trend_filter_ma", 200)), step=10)
        max_hold  = c2.number_input("Max Hold Days", min_value=1, max_value=20,
                                    value=int(r2c.get("max_hold_days", 5)), step=1)

        st.subheader("Position Sizing")
        s1, s2 = st.columns(2)
        size_pct = s1.number_input("Position Size (% of equity)", min_value=1.0, max_value=25.0,
                                   value=float(r2c.get("position_size_pct", 5.0)), step=0.5, format="%.1f")
        equity   = s2.number_input("Paper Starting Equity ($)", min_value=1000.0, max_value=1_000_000.0,
                                   value=float(r2c.get("paper_starting_equity", 25000.0)), step=1000.0, format="%.0f")

        st.subheader("Frictions (backtest)")
        f1, f2 = st.columns(2)
        commission = f1.number_input("Commission per Share ($)", min_value=0.0, max_value=0.10,
                                     value=float(r2c.get("commission_per_share", 0.0)), step=0.005, format="%.3f")
        slippage   = f2.number_input("Slippage per Fill (%)", min_value=0.0, max_value=1.0,
                                     value=float(r2c.get("slippage_pct", 0.05)), step=0.01, format="%.2f")

        if st.form_submit_button("Save RSI2 Parameters", type="primary"):
            cfg["rsi2"] = {
                "rsi_period":            int(r2c.get("rsi_period", 2)),
                "entry_rsi_max":         entry_max,
                "exit_rsi_min":          exit_min,
                "trend_filter_ma":       trend_ma,
                "max_hold_days":         max_hold,
                "position_size_pct":     size_pct,
                "paper_starting_equity": equity,
                "commission_per_share":  commission,
                "slippage_pct":          slippage,
            }
            _save_toml(path, cfg)  # preserves [universe] since cfg was loaded whole
            st.toast(f"RSI2 parameters saved to {path}", icon="✅")


@st.fragment
def _config_strategy_tab() -> None:
    st.subheader("Strategy Parameters")
    which = st.selectbox("Strategy", sorted(list_strategies()), key="cfg_strategy")
    if which == "wheel":
        _wheel_params_form()
    elif which == "put_credit_spread":
        _pcs_params_form()
    elif which == "rsi2":
        _rsi2_params_form()
    else:
        st.info(f"No editor for '{which}' yet — edit its TOML in config/ directly.")


@st.fragment
def _config_scheduler_tab() -> None:
    st.subheader("Daily Scheduler")

    routed = ROUTER_PATH.exists()
    if routed:
        # Router mode: scheduler.py reads [scheduler] from strategies.toml,
        # and the ticker universe comes from [router.assignments]
        st.caption("Router mode — settings saved to `config/strategies.toml`. "
                   "Tickers come from the **Router** tab assignments, not a list here. "
                   "Register unattended runs with `scripts/register_daily_task.ps1`.")
        cfg = _router_cfg()
        cfg_path: str | Path = ROUTER_PATH
    else:
        st.caption("Legacy mode — settings saved to `config/wheel.toml` "
                   "(create config/strategies.toml to enable multi-strategy routing).")
        cfg = _load_toml(CONFIG_PATH)
        cfg_path = CONFIG_PATH
    sched = cfg.get("scheduler", {})

    with st.form("scheduler_form"):
        sched_tickers = sched.get("tickers", [])
        if not routed:
            all_tracked = tracked_tickers(states_data(), signals_data())
            # Allow free-text entry for new tickers not yet in state
            sched_tickers = st.multiselect(
                "Tickers to scan daily",
                options=sorted(set(all_tracked) | set(sched_tickers)),
                default=sched_tickers,
            )
        adapter = st.radio("Adapter", ["alpaca", "fixture"],
                           index=0 if sched.get("adapter", "alpaca") == "alpaca" else 1,
                           horizontal=True)

        s1, s2 = st.columns(2)
        run_hour   = s1.number_input("Run Hour (24h ET)",   min_value=0, max_value=23,
                                      value=int(sched.get("run_hour", 9)))
        run_minute = s2.number_input("Run Minute",          min_value=0, max_value=59,
                                      value=int(sched.get("run_minute", 35)))
        timezone   = st.text_input("Timezone", value=sched.get("timezone", "America/New_York"))

        if st.form_submit_button("Save Scheduler Settings", type="primary"):
            new_sched = {
                "adapter":    adapter,
                "run_hour":   run_hour,
                "run_minute": run_minute,
                "timezone":   timezone,
            }
            if not routed:
                new_sched["tickers"] = sched_tickers
            cfg["scheduler"] = new_sched
            if routed:
                _save_router_cfg(cfg)
            else:
                _save_toml(cfg_path, cfg)
            st.toast(f"Scheduler settings saved to {cfg_path}.", icon="✅")


@st.fragment
def _config_tickers_tab() -> None:
    st.subheader("Tracked Tickers")
    states = states_data()

    if states:
        rows = []
        for t, s in sorted(states.items()):
            rows.append({
                "Ticker":          t,
                "Phase":           s.get("phase", "A"),
                "Shares":          s.get("shares_held", 0),
                "Cost Basis":      s.get("cost_basis"),
                "Assignment Date": s.get("assignment_date") or "—",
            })
        st.dataframe(
            pd.DataFrame(rows), use_container_width=True, hide_index=True,
            column_config={
                "Cost Basis": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
    else:
        st.info("No tickers in state file yet.")

    st.divider()

    col_add, col_remove = st.columns(2)

    with col_add:
        st.markdown("**Add Ticker**")
        with st.form("add_ticker_form"):
            new_ticker = st.text_input("Symbol (e.g. TSLA)").upper().strip()
            new_phase  = st.radio("Starting Phase", ["A", "B"], horizontal=True)
            cost_basis = None
            if new_phase == "B":
                cost_basis = st.number_input("Cost Basis", min_value=0.01, step=0.01)
            if st.form_submit_button("Add Ticker", type="primary"):
                if not new_ticker:
                    st.warning("Enter a ticker symbol.")
                elif new_ticker in states:
                    st.warning(f"{new_ticker} is already tracked.")
                else:
                    entry = {
                        "phase": new_phase, "shares_held": 100 if new_phase == "B" else 0,
                        "cost_basis": round(cost_basis, 4) if cost_basis else None,
                        "assignment_date": date.today().isoformat() if new_phase == "B" else None,
                        "open_put_strike": None, "open_put_expiry": None,
                    }
                    set_ticker_state(STATE_PATH, new_ticker, entry)
                    st.toast(f"Added {new_ticker} (Phase {new_phase})", icon="✅")
                    st.rerun(scope="fragment")

    with col_remove:
        st.markdown("**Remove Ticker**")
        with st.form("remove_ticker_form"):
            ticker_to_remove = st.selectbox("Ticker", options=sorted(states.keys()) or ["—"])
            if st.form_submit_button("Remove Ticker", type="secondary"):
                if ticker_to_remove and ticker_to_remove != "—":
                    all_states = get_all_ticker_states(STATE_PATH)
                    all_states.pop(ticker_to_remove, None)
                    from src.state_store import write_state
                    write_state(STATE_PATH, all_states)
                    st.toast(f"Removed {ticker_to_remove}", icon="✅")
                    st.rerun(scope="fragment")


def page_configuration() -> None:
    st.title("Configuration")

    tab_strategy, tab_router, tab_scheduler, tab_tickers = st.tabs([
        "Strategy Parameters", "Router", "Scheduler", "Tracked Tickers"
    ])
    with tab_strategy:
        _config_strategy_tab()
    with tab_router:
        _config_router_tab()
    with tab_scheduler:
        _config_scheduler_tab()
    with tab_tickers:
        _config_tickers_tab()


# ---------------------------------------------------------------------------
# Main — st.navigation multipage (no radio/_pending_nav rerun hack)
# ---------------------------------------------------------------------------

PAGE_DASHBOARD     = st.Page(page_dashboard,     title="Dashboard",     icon="📊", url_path="dashboard", default=True)
PAGE_POSITIONS     = st.Page(page_positions,     title="Positions",     icon="💼", url_path="positions")
PAGE_TICKER_DETAIL = st.Page(page_ticker_detail, title="Ticker Detail", icon="🔍", url_path="ticker_detail")
PAGE_RUN_SIGNALS   = st.Page(page_run_signals,   title="Run Signals",   icon="⚡", url_path="run_signals")
PAGE_BACKTEST      = st.Page(page_backtest,      title="Backtest",      icon="📈", url_path="backtest")
PAGE_CONFIGURATION = st.Page(page_configuration, title="Configuration", icon="⚙️", url_path="configuration")


def main() -> None:
    st.set_page_config(
        page_title="Claude Trader",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.sidebar.title("Claude Trader")
    st.sidebar.caption("Multi-strategy signal system")

    nav = st.navigation([
        PAGE_DASHBOARD,
        PAGE_POSITIONS,
        PAGE_TICKER_DETAIL,
        PAGE_RUN_SIGNALS,
        PAGE_BACKTEST,
        PAGE_CONFIGURATION,
    ])
    nav.run()


if __name__ == "__main__":
    main()
