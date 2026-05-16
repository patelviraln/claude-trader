#!/usr/bin/env python3
"""
Wheel Trader Dashboard — FastAPI web UI.

Usage:
    python app.py                          # default port 8000
    python app.py --port 8080
    uvicorn app:app --reload               # dev mode with auto-reload
"""
from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.dashboard_data import (
    get_all_ticker_states,
    get_all_tracked_tickers,
    get_iv_series,
    get_last_signal,
    get_recent_signals,
)
from src.state_store import get_ticker_state, set_ticker_state
from src.logger import setup_logging

import structlog

log = structlog.get_logger()

app = FastAPI(title="Wheel Trader Dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")

STATE_PATH = "wheel_state.json"
SIGNALS_PATH = "signals.jsonl"
IV_DIR = "iv_history"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, msg: str = "", msg_type: str = "ok"):
    tickers = get_all_tracked_tickers(STATE_PATH, SIGNALS_PATH)
    states = get_all_ticker_states(STATE_PATH)
    last_signals = {t: get_last_signal(t, SIGNALS_PATH) for t in tickers}
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "tickers": tickers,
            "states": states,
            "last_signals": last_signals,
            "message": msg,
            "message_type": msg_type,
        },
    )


# ---------------------------------------------------------------------------
# Ticker detail
# ---------------------------------------------------------------------------

@app.get("/ticker/{ticker}", response_class=HTMLResponse)
async def ticker_detail(request: Request, ticker: str):
    ticker = ticker.upper()
    state = get_ticker_state(STATE_PATH, ticker)
    signals = get_recent_signals(SIGNALS_PATH, ticker=ticker, n=50)
    last_signal = signals[0] if signals else None
    iv_series = get_iv_series(ticker, IV_DIR)
    iv_dates = [e["date"] for e in iv_series]
    iv_values = [e["iv"] for e in iv_series]
    return templates.TemplateResponse(
        request=request,
        name="ticker.html",
        context={
            "ticker": ticker,
            "state": state,
            "signals": signals,
            "last_signal": last_signal,
            "iv_series": iv_series,
            "iv_dates": iv_dates,
            "iv_values": iv_values,
        },
    )


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------

@app.post("/transition/assign")
async def assign(ticker: str = Form(...), cost_basis: float = Form(...)):
    ticker = ticker.upper()
    existing = get_ticker_state(STATE_PATH, ticker)
    if existing.get("phase") == "B":
        return RedirectResponse(f"/?msg={ticker}+is+already+Phase+B&msg_type=err", status_code=303)
    from datetime import date
    new_state = {
        "phase": "B",
        "shares_held": 100,
        "cost_basis": round(cost_basis, 4),
        "assignment_date": date.today().isoformat(),
        "open_put_strike": None,
        "open_put_expiry": None,
    }
    set_ticker_state(STATE_PATH, ticker, new_state)
    log.info("dashboard.assigned", ticker=ticker, cost_basis=cost_basis)
    return RedirectResponse(f"/ticker/{ticker}?msg=Assigned+{ticker}+to+Phase+B&msg_type=ok", status_code=303)


@app.post("/transition/exit")
async def exit_position(ticker: str = Form(...)):
    ticker = ticker.upper()
    existing = get_ticker_state(STATE_PATH, ticker)
    if existing.get("phase") == "A":
        return RedirectResponse(f"/?msg={ticker}+is+already+Phase+A&msg_type=err", status_code=303)
    new_state = {
        "phase": "A",
        "shares_held": 0,
        "cost_basis": None,
        "assignment_date": None,
        "open_put_strike": None,
        "open_put_expiry": None,
    }
    set_ticker_state(STATE_PATH, ticker, new_state)
    log.info("dashboard.exited", ticker=ticker)
    return RedirectResponse(f"/ticker/{ticker}", status_code=303)


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Wheel Trader Dashboard")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    setup_logging("INFO")
    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)
