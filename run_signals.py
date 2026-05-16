#!/usr/bin/env python3
"""
CLI entry point for the Wheel strategy signal system.

Usage:
    python run_signals.py --tickers AAPL MSFT --phase A
    python run_signals.py --tickers AAPL --adapter fixture
    python run_signals.py --tickers AAPL MSFT NVDA --adapter alpaca
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

# Force UTF-8 on Windows stdout to handle any non-ASCII in signal cards
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from src.logger import setup_logging
from src.signal_engine import run_signals_batch
from src.state_store import set_ticker_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wheel Strategy Signal Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        required=True,
        metavar="TICKER",
        help="One or more ticker symbols to scan",
    )
    parser.add_argument(
        "--adapter",
        choices=["fixture", "alpaca"],
        default="fixture",
        help="Data adapter to use (default: fixture)",
    )
    parser.add_argument(
        "--phase",
        choices=["A", "B"],
        default=None,
        help="Override phase for all tickers (A=no shares, B=100 shares held)",
    )
    parser.add_argument(
        "--config",
        default="config/wheel.toml",
        help="Path to wheel config TOML (default: config/wheel.toml)",
    )
    parser.add_argument(
        "--state",
        default="wheel_state.json",
        help="Path to wheel state JSON (default: wheel_state.json)",
    )
    parser.add_argument(
        "--signals-out",
        default="signals.jsonl",
        help="Path to JSONL output file (default: signals.jsonl)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Output raw JSON cards to stdout; suppress rich terminal display",
    )
    return parser.parse_args()


def build_adapter(adapter_name: str):
    if adapter_name == "fixture":
        from src.adapters.fixture_adapter import FixtureAdapter
        return FixtureAdapter()
    elif adapter_name == "alpaca":
        from src.adapters.alpaca_adapter import AlpacaPaperAdapter
        return AlpacaPaperAdapter()
    else:
        print(f"Unknown adapter: {adapter_name}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)

    # Override phase in state file if --phase flag is set
    if args.phase:
        state_path = Path(args.state)
        for ticker in args.tickers:
            from src.state_store import get_ticker_state
            existing = get_ticker_state(state_path, ticker)
            if not existing:
                existing = {"phase": args.phase, "shares_held": 0, "cost_basis": None,
                            "assignment_date": None, "open_put_strike": None, "open_put_expiry": None}
            else:
                existing["phase"] = args.phase
            set_ticker_state(state_path, ticker, existing)

    adapter = build_adapter(args.adapter)
    cards = run_signals_batch(
        tickers=args.tickers,
        adapter=adapter,
        config_path=args.config,
        state_path=args.state,
        signals_path=args.signals_out,
        print_output=not args.json_only,
    )

    if args.json_only:
        for card in cards:
            print(card.to_json())


if __name__ == "__main__":
    main()
