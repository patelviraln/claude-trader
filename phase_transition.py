#!/usr/bin/env python3
"""
Wheel mode transition CLI (Sell Put <-> Covered Call).

Internally the modes are stored as phase "A" (Sell Put — no shares, selling
cash-secured puts) and "B" (Covered Call — 100 shares held, selling calls).

Commands:
    assign  — put was assigned; move ticker from Sell Put -> Covered Call
    exit    — call was exercised or expired worthless; move ticker back to Sell Put

Usage:
    python phase_transition.py assign NVDA --cost-basis 880.50
    python phase_transition.py exit NVDA
    python phase_transition.py assign AAPL --cost-basis 175.00 --state custom_state.json
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.logger import setup_logging
from src.state_store import get_ticker_state, set_ticker_state
import structlog

log = structlog.get_logger()


def cmd_assign(args: argparse.Namespace) -> None:
    """Move ticker from Phase A -> B: put was assigned, now holding 100 shares."""
    state_path = Path(args.state)
    ticker = args.ticker.upper()
    existing = get_ticker_state(state_path, ticker)

    if existing.get("phase") == "B":
        log.warning(
            "transition.already_in_phase_b",
            ticker=ticker,
            msg="Ticker is already in Phase B — no change made",
        )
        print(f"[warn] {ticker} is already in Covered Call mode. Use 'exit' first if re-assigning.",
              file=sys.stderr)
        return

    new_state = {
        "phase": "B",
        "shares_held": 100,
        "cost_basis": round(args.cost_basis, 4),
        "assignment_date": date.today().isoformat(),
        "open_put_strike": None,
        "open_put_expiry": None,
    }
    set_ticker_state(state_path, ticker, new_state)
    log.info(
        "transition.assigned",
        ticker=ticker,
        cost_basis=new_state["cost_basis"],
        assignment_date=new_state["assignment_date"],
    )
    print(
        f"[ok] {ticker}: Sell Put -> Covered Call  |  100 shares @ ${args.cost_basis:.2f}  "
        f"|  assigned {new_state['assignment_date']}"
    )


def cmd_exit(args: argparse.Namespace) -> None:
    """Move ticker from Phase B -> A: call exercised or expired, shares cleared."""
    state_path = Path(args.state)
    ticker = args.ticker.upper()
    existing = get_ticker_state(state_path, ticker)

    if existing.get("phase") == "A":
        log.warning(
            "transition.already_in_phase_a",
            ticker=ticker,
            msg="Ticker is already in Phase A — no change made",
        )
        print(f"[warn] {ticker} is already in Sell Put mode.", file=sys.stderr)
        return

    new_state = {
        "phase": "A",
        "shares_held": 0,
        "cost_basis": None,
        "assignment_date": None,
        "open_put_strike": None,
        "open_put_expiry": None,
    }
    set_ticker_state(state_path, ticker, new_state)
    log.info("transition.exited", ticker=ticker)
    print(f"[ok] {ticker}: Covered Call -> Sell Put  |  shares cleared, ready to sell puts")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wheel Mode Transition (Sell Put <-> Covered Call)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--state", default="wheel_state.json", help="Path to state JSON")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(dest="command", required=True)

    p_assign = sub.add_parser("assign", help="Put assigned: Sell Put -> Covered Call")
    p_assign.add_argument("ticker", help="Ticker symbol, e.g. NVDA")
    p_assign.add_argument(
        "--cost-basis",
        type=float,
        required=True,
        help="Assignment price per share (strike price)",
    )

    p_exit = sub.add_parser("exit", help="Call exercised/expired: Covered Call -> Sell Put")
    p_exit.add_argument("ticker", help="Ticker symbol, e.g. NVDA")

    args = parser.parse_args()
    setup_logging(args.log_level)

    if args.command == "assign":
        cmd_assign(args)
    elif args.command == "exit":
        cmd_exit(args)


if __name__ == "__main__":
    main()
