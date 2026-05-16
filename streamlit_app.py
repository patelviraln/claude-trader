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
CONFIG_PATH  = "config/wheel.toml"

PHASE_COLORS  = {"A": "#58a6ff", "B": "#bc8cff"}
SIGNAL_COLORS = {"SELL_PUT": "#3fb950", "SELL_CALL": "#bc8cff", "NO_SIGNAL": "#8b949e"}
TIER_COLORS   = {"HIGH": "#3fb950", "MEDIUM": "#d29922", "LOW": "#f85149"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
            sig_phase    = sig.get("phase", "—") if sig else "—"
            sig_color    = SIGNAL_COLORS.get(sig_phase, "#8b949e")
            signal_badge = _badge(sig_phase.replace("_", " "), sig_color) if sig else ""

            st.markdown(
                f"### {ticker} &nbsp; {phase_badge} &nbsp; {signal_badge}",
                unsafe_allow_html=True,
            )

            if sig:
                c1, c2 = st.columns(2)
                c1.metric("Price",      f"${sig.get('underlying_price', 0):.2f}")
                c1.metric("Option Mid", f"${sig.get('option_mid') or 0:.2f}" if sig.get("option_mid") else "—")
                iv = sig.get("iv_rank")
                c2.metric("IV Rank", f"{iv:.1f}%" if iv is not None else "—")
                tier = sig.get("confidence_tier")
                if tier:
                    c2.markdown(_badge(tier, TIER_COLORS.get(tier, "#8b949e")), unsafe_allow_html=True)
                if sig.get("recommended_strike"):
                    st.caption(f"Strike ${sig['recommended_strike']:.2f}  ·  Expiry {sig.get('recommended_expiry','—')}  ·  DTE {sig.get('dte','—')}")
                if sig.get("order_id"):
                    st.caption(f"Order: {sig['order_id'][:12]}…  [{sig.get('order_status','—')}]")
            else:
                st.caption("No signals yet")

            if state.get("cost_basis"):
                st.caption(f"Cost basis ${state['cost_basis']:.2f}  ·  {state.get('assignment_date','')}")

            st.link_button("View Details", f"/?page=ticker&t={ticker}", use_container_width=True)
            st.divider()


# ---------------------------------------------------------------------------
# Page: Ticker Detail
# ---------------------------------------------------------------------------

def page_ticker_detail() -> None:
    tickers = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
    if not tickers:
        st.info("No tickers tracked yet.")
        return

    # Allow pre-selection via query param
    params  = st.query_params
    default = params.get("t", tickers[0]) if tickers else None
    default_idx = tickers.index(default) if default in tickers else 0

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
        iv = last_sig.get("iv_rank")
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
                "Signal":     s.get("phase", ""),
                "Strike":     f"${s['recommended_strike']:.2f}" if s.get("recommended_strike") else "—",
                "Expiry":     s.get("recommended_expiry", "—"),
                "Delta":      f"{s['delta_estimate']:+.3f}" if s.get("delta_estimate") is not None else "—",
                "IV Rank":    f"{s['iv_rank']:.1f}%" if s.get("iv_rank") is not None else "—",
                "Mid":        f"${s['option_mid']:.2f}" if s.get("option_mid") else "—",
                "Confidence": s.get("confidence_tier", "—"),
                "Order":      (s.get("order_id") or "")[:8] or "—",
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
    sched   = cfg.get("scheduler", {})
    tracked = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
    default_tickers = sched.get("tickers", tracked[:3] if tracked else [])

    with st.form("run_form"):
        tickers  = st.multiselect("Tickers", options=tracked or default_tickers, default=[t for t in default_tickers if t in tracked] or default_tickers)
        adapter  = st.radio("Adapter", ["alpaca", "fixture"], horizontal=True)
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
                    color = SIGNAL_COLORS.get(card.phase, "#8b949e")
                    tier_color = TIER_COLORS.get(card.confidence_tier or "", "#8b949e")
                    if card.phase == "NO_SIGNAL":
                        st.error(f"**{card.ticker}** — NO SIGNAL: {card.no_signal_reason}")
                    else:
                        cols = st.columns([2, 1, 1, 1, 1, 2])
                        cols[0].markdown(_badge(card.phase.replace("_", " "), color), unsafe_allow_html=True)
                        cols[0].markdown(f"**{card.ticker}**")
                        cols[1].metric("Strike",  f"${card.recommended_strike:.2f}" if card.recommended_strike else "—")
                        cols[2].metric("Mid",     f"${card.option_mid:.2f}" if card.option_mid else "—")
                        cols[3].metric("IV Rank", f"{card.iv_rank:.1f}%" if card.iv_rank is not None else "—")
                        cols[4].markdown(_badge(card.confidence_tier or "—", tier_color), unsafe_allow_html=True)
                        if card.order_id:
                            cols[5].success(f"Order {card.order_id[:8]}… [{card.order_status}]")
                        if card.thesis_text:
                            st.caption(f"Thesis: {card.thesis_text}")
                        st.divider()
            except Exception as exc:
                st.error(f"Scan failed: {exc}")


# ---------------------------------------------------------------------------
# Page: Configuration (Phase 7)
# ---------------------------------------------------------------------------

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

    page = st.sidebar.radio(
        "Navigate",
        ["Dashboard", "Ticker Detail", "Run Signals", "Configuration"],
        label_visibility="collapsed",
    )

    st.sidebar.divider()
    st.sidebar.caption("Phase 5 · Dashboard")
    st.sidebar.caption("Phase 7 · Configuration")

    if page == "Dashboard":
        page_dashboard()
    elif page == "Ticker Detail":
        page_ticker_detail()
    elif page == "Run Signals":
        page_run_signals()
    elif page == "Configuration":
        page_configuration()


if __name__ == "__main__":
    main()
