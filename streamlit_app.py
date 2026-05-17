#!/usr/bin/env python3
"""
Wheel Trader — Streamlit dashboard.

Run:
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import json
import tomllib
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import tomli_w
from dotenv import load_dotenv

load_dotenv()

from src.dashboard_data import (
    get_all_ticker_states,
    get_all_tracked_tickers,
    get_iv_series,
    get_last_signal,
    get_recent_signals,
)
from src.state_store import get_ticker_state, set_ticker_state

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_PATH   = "wheel_state.json"
SIGNALS_PATH = "signals.jsonl"
IV_DIR       = "iv_history"
CONFIG_PATH  = "config/wheel.toml"   # default strategy config (Wheel)

PHASE_COLORS  = {"A": "#58a6ff", "B": "#bc8cff"}
SIGNAL_COLORS = {
    "SELL_PUT": "#3fb950", "SELL_CALL": "#bc8cff", "NO_SIGNAL": "#8b949e",
    "SELL_PUT_SPREAD": "#58a6ff", "BUY_EQUITY": "#79c0ff", "SELL_EQUITY": "#f85149",
}
TIER_COLORS   = {"HIGH": "#3fb950", "MEDIUM": "#d29922", "LOW": "#f85149"}

FIXTURES_DIR = Path("fixtures")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixture_tickers() -> list[str]:
    """Return tickers that have both ohlcv and options fixture files."""
    if not FIXTURES_DIR.exists():
        return []
    ohlcv   = {p.stem.replace("_ohlcv", "") for p in FIXTURES_DIR.glob("*_ohlcv.json")}
    options = {p.stem.replace("_options", "") for p in FIXTURES_DIR.glob("*_options.json")}
    return sorted(ohlcv & options)


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

def _load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)

def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "wb") as f:
        tomli_w.dump(cfg, f)

# ---------------------------------------------------------------------------
# Page: Dashboard
# ---------------------------------------------------------------------------

def page_dashboard() -> None:
    st.title("Dashboard")

    tickers = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
    if not tickers:
        st.info("No tickers tracked yet. Use **Configuration → Tickers** to add one.")
        return

    states      = get_all_ticker_states(STATE_PATH)
    last_sigs   = {t: get_last_signal(t, SIGNALS_PATH) for t in tickers}

    cols = st.columns(3)
    for i, ticker in enumerate(tickers):
        state = states.get(ticker, {})
        sig   = last_sigs.get(ticker)
        phase = state.get("phase", "A")

        with cols[i % 3]:
            phase_badge  = _badge(f"Phase {phase}", PHASE_COLORS.get(phase, "#8b949e"))
            sig_phase    = sig.get("signal_type", "—") if sig else "—"
            sig_color    = SIGNAL_COLORS.get(sig_phase, "#8b949e")
            signal_badge = _badge(sig_phase.replace("_", " "), sig_color) if sig else ""

            st.markdown(
                f"### {ticker} &nbsp; {phase_badge} &nbsp; {signal_badge}",
                unsafe_allow_html=True,
            )

            if sig:
                c1, c2 = st.columns(2)
                _legs = sig.get("legs") or []
                _leg0 = _legs[0] if _legs else {}
                _payload = sig.get("payload") or {}
                c1.metric("Price",      f"${sig.get('underlying_price', 0):.2f}")
                _mid = _leg0.get("limit_price")
                c1.metric("Option Mid", f"${_mid:.2f}" if _mid else "—")
                iv = _payload.get("iv_rank")
                c2.metric("IV Rank", f"{iv:.1f}%" if iv is not None else "—")
                tier = sig.get("confidence_tier")
                if tier:
                    c2.markdown(_badge(tier, TIER_COLORS.get(tier, "#8b949e")), unsafe_allow_html=True)
                if _leg0.get("strike"):
                    st.caption(f"Strike ${_leg0['strike']:.2f}  ·  Expiry {_leg0.get('expiry','—')}  ·  DTE {_payload.get('dte','—')}")
                _order_ids = sig.get("order_ids") or []
                _order_sts = sig.get("order_statuses") or []
                if _order_ids:
                    st.caption(f"Order: {_order_ids[0][:12]}…  [{_order_sts[0] if _order_sts else '—'}]")
            else:
                st.caption("No signals yet")

            if state.get("cost_basis"):
                st.caption(f"Cost basis ${state['cost_basis']:.2f}  ·  {state.get('assignment_date','')}")

            if st.button("View Details", key=f"detail_{ticker}", use_container_width=True):
                st.session_state["_pending_nav"] = "Ticker Detail"
                st.session_state["_detail_ticker"] = ticker
                st.rerun()
            st.divider()


# ---------------------------------------------------------------------------
# Page: Ticker Detail
# ---------------------------------------------------------------------------

def page_ticker_detail() -> None:
    tickers = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
    if not tickers:
        st.info("No tickers tracked yet.")
        return

    preselect = st.session_state.pop("_detail_ticker", None)
    default_idx = tickers.index(preselect) if preselect in tickers else 0

    ticker = st.selectbox("Ticker", tickers, index=default_idx)
    st.title(ticker)

    state      = get_ticker_state(STATE_PATH, ticker)
    signals    = get_recent_signals(SIGNALS_PATH, ticker=ticker, n=50)
    last_sig   = signals[0] if signals else None
    iv_series  = get_iv_series(ticker, IV_DIR)

    # --- Stats row ---
    phase = state.get("phase", "A")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Phase",       phase)
    c2.metric("Shares Held", state.get("shares_held", 0))
    if state.get("cost_basis"):
        c3.metric("Cost Basis", f"${state['cost_basis']:.2f}")
    if last_sig:
        c4.metric("Last Price", f"${last_sig.get('underlying_price', 0):.2f}")
        iv = (last_sig.get("payload") or {}).get("iv_rank")
        c5.metric("IV Rank", f"{iv:.1f}%" if iv is not None else "—")

    # --- IV History chart ---
    if iv_series:
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
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No IV history yet — run a signal scan first.")

    # --- Signal history table ---
    st.subheader("Signal History")
    if signals:
        rows = []
        for s in signals:
            rows.append({
                "Time":       s.get("signal_timestamp", "")[:16].replace("T", " "),
                _legs = s.get("legs") or []
                _leg0 = _legs[0] if _legs else {}
                _payload = s.get("payload") or {}
                _order_ids = s.get("order_ids") or []
                "Signal":     s.get("signal_type", ""),
                "Strike":     f"${_leg0['strike']:.2f}" if _leg0.get("strike") else "—",
                "Expiry":     _leg0.get("expiry", "—"),
                "Delta":      f"{_leg0['delta_estimate']:+.3f}" if _leg0.get("delta_estimate") is not None else "—",
                "IV Rank":    f"{_payload['iv_rank']:.1f}%" if _payload.get("iv_rank") is not None else "—",
                "Mid":        f"${_leg0['limit_price']:.2f}" if _leg0.get("limit_price") else "—",
                "Confidence": s.get("confidence_tier", "—"),
                "Order":      (_order_ids[0][:8] if _order_ids else "") or "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info(f"No signals recorded for {ticker}.")

    # --- Phase transition ---
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
                st.success(f"{ticker} moved to Phase B @ ${cost:.2f}")
                st.rerun()
    else:
        if st.button("Record Exit  (B → A)", type="secondary"):
            set_ticker_state(STATE_PATH, ticker, {
                "phase": "A", "shares_held": 0, "cost_basis": None,
                "assignment_date": None, "open_put_strike": None, "open_put_expiry": None,
            })
            st.success(f"{ticker} moved back to Phase A")
            st.rerun()


# ---------------------------------------------------------------------------
# Page: Run Signals
# ---------------------------------------------------------------------------

def page_run_signals() -> None:
    st.title("Run Signals")
    st.caption("Scan tickers through the 7-filter pipeline and optionally place paper orders.")

    cfg     = _load_config()
    tracked = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
    fix_tickers = _fixture_tickers()
    adapters = (["alpaca"] if _alpaca_available() else []) + ["fixture"]

    adapter = st.radio("Adapter", adapters, horizontal=True, key="rs_adapter")
    if adapter == "fixture":
        pool = [t for t in tracked if t in fix_tickers] or fix_tickers
        if not pool:
            st.warning("No fixture files found in fixtures/.")
        missing = [t for t in tracked if t not in fix_tickers]
        if missing:
            st.caption(f"No fixture for: {', '.join(missing)} — add fixture files or use Alpaca.")
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
                from src.adapters.fixture_adapter import FixtureAdapter
                if adapter == "fixture":
                    data_adapter = FixtureAdapter()
                else:
                    from src.adapters.alpaca_adapter import AlpacaPaperAdapter
                    data_adapter = AlpacaPaperAdapter()

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
                )

                for card in cards:
                    color = SIGNAL_COLORS.get(card.signal_type, "#8b949e")
                    tier_color = TIER_COLORS.get(card.confidence_tier or "", "#8b949e")
                    if card.signal_type == "NO_SIGNAL":
                        st.error(f"**{card.ticker}** — NO SIGNAL: {card.no_signal_reason}")
                    else:
                        leg0 = card.legs[0] if card.legs else None
                        cols = st.columns([2, 1, 1, 1, 1, 2])
                        cols[0].markdown(_badge(card.signal_type.replace("_", " "), color), unsafe_allow_html=True)
                        cols[0].markdown(f"**{card.ticker}**")
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


# ---------------------------------------------------------------------------
# Page: Configuration (Phase 7)
# ---------------------------------------------------------------------------

def page_backtest() -> None:
    st.title("Backtest")
    st.caption("Walk-forward backtest using 30-day realized vol + Black-Scholes pricing.")

    cfg = _load_config()
    wheel_cfg = cfg.get("wheel", cfg)
    tracked   = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
    fix_tickers = _fixture_tickers()
    adapters  = (["alpaca"] if _alpaca_available() else []) + ["fixture"]

    col1, col2 = st.columns(2)
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
        phase   = st.radio("Phase", ["A — Sell Put", "B — Sell Call"], horizontal=True)
        run     = st.form_submit_button("Run Backtest", type="primary")

    if run:
        phase_key = "A" if phase.startswith("A") else "B"
        with st.spinner(f"Running backtest for {ticker} ({start} → {end})…"):
            try:
                from src.backtester import BacktestEngine
                from src.adapters.fixture_adapter import FixtureAdapter
                if adapter == "fixture":
                    data_adapter = FixtureAdapter()
                else:
                    from src.adapters.alpaca_adapter import AlpacaPaperAdapter
                    data_adapter = AlpacaPaperAdapter()

                engine  = BacktestEngine(data_adapter, {"wheel": wheel_cfg})
                summary = engine.run(ticker, start, end, phase=phase_key)

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Total trades",    summary.total_trades)
                m2.metric("Closed trades",   summary.closed_trades)
                m3.metric("Win rate",        f"{summary.win_rate:.1f}%")
                m4.metric("Total premium",   f"${summary.total_premium:.2f}")
                m5.metric("Total P&L",       f"${summary.total_pnl:.2f}")

                if summary.trades:
                    closed = [t for t in summary.trades if t.outcome != "OPEN"]
                    if closed:
                        cumulative = []
                        running = 0.0
                        for t in closed:
                            running += t.pnl
                            cumulative.append({"date": t.expiry_date, "pnl": round(running, 2)})
                        eq_df = pd.DataFrame(cumulative)
                        fig = go.Figure(go.Scatter(x=eq_df["date"], y=eq_df["pnl"],
                                                   mode="lines+markers", name="Equity"))
                        fig.update_layout(title="Cumulative P&L", xaxis_title="Expiry",
                                          yaxis_title="P&L ($/share)", height=350)
                        st.plotly_chart(fig, use_container_width=True)

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
                    st.dataframe(pd.DataFrame(rows), use_container_width=True)
                else:
                    st.info("No trades generated — try a wider date range or relaxed config.")
            except Exception as exc:
                st.error(f"Backtest failed: {exc}")


def page_configuration() -> None:
    st.title("Configuration")

    cfg   = _load_config()
    wheel = cfg.get("wheel", {})
    sched = cfg.get("scheduler", {})

    tab_strategy, tab_scheduler, tab_tickers = st.tabs([
        "Strategy Parameters", "Scheduler", "Tracked Tickers"
    ])

    # ---- Strategy Parameters ----
    with tab_strategy:
        st.subheader("Filter Parameters")
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

            if st.form_submit_button("Save Strategy Parameters", type="primary"):
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
                _save_config(cfg)
                st.success("Strategy parameters saved to config/wheel.toml")

    # ---- Scheduler ----
    with tab_scheduler:
        st.subheader("Daily Scheduler")
        st.caption("Controls the `python scheduler.py` cron job.")

        with st.form("scheduler_form"):
            all_tracked = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
            current_tickers = sched.get("tickers", [])

            # Allow free-text entry for new tickers not yet in state
            sched_tickers = st.multiselect(
                "Tickers to scan daily",
                options=sorted(set(all_tracked) | set(current_tickers)),
                default=current_tickers,
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
                cfg["scheduler"] = {
                    "tickers":    sched_tickers,
                    "adapter":    adapter,
                    "run_hour":   run_hour,
                    "run_minute": run_minute,
                    "timezone":   timezone,
                }
                _save_config(cfg)
                st.success("Scheduler settings saved.")

    # ---- Tickers ----
    with tab_tickers:
        st.subheader("Tracked Tickers")
        states = get_all_ticker_states(STATE_PATH)

        if states:
            rows = []
            for t, s in sorted(states.items()):
                rows.append({
                    "Ticker":          t,
                    "Phase":           s.get("phase", "A"),
                    "Shares":          s.get("shares_held", 0),
                    "Cost Basis":      f"${s['cost_basis']:.2f}" if s.get("cost_basis") else "—",
                    "Assignment Date": s.get("assignment_date") or "—",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
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
                        st.success(f"Added {new_ticker} (Phase {new_phase})")
                        st.rerun()

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
                        st.success(f"Removed {ticker_to_remove}")
                        st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Wheel Trader",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.sidebar.title("Wheel Trader")
    st.sidebar.caption("Wheel options strategy system")
    st.sidebar.divider()

    pages = ["Dashboard", "Ticker Detail", "Run Signals", "Backtest", "Configuration"]

    # Programmatic navigation — must be applied BEFORE the radio widget is instantiated
    if "_pending_nav" in st.session_state:
        st.session_state["_nav"] = st.session_state.pop("_pending_nav")

    page = st.sidebar.radio(
        "Navigate",
        pages,
        key="_nav",
        label_visibility="collapsed",
    )

    st.sidebar.divider()

    if page == "Dashboard":
        page_dashboard()
    elif page == "Ticker Detail":
        page_ticker_detail()
    elif page == "Run Signals":
        page_run_signals()
    elif page == "Backtest":
        page_backtest()
    elif page == "Configuration":
        page_configuration()


if __name__ == "__main__":
    main()
