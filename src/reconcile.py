"""
Fill-to-state reconciliation: make per-strategy state files agree with the
live paper account before every scan.

Without this the loop double-enters: an RSI2 buy fills but state/rsi2.json
still says flat, so the next scan emits another BUY_EQUITY. Reconciliation
derives the truth from open positions:

  rsi2   — long equity on an rsi2-routed ticker  → in_position/shares/entry
  pcs    — short+long put pair on a pcs ticker   → open_spread + strikes
  wheel  — short put → open_put_strike/expiry; >=100 long shares → Phase B
           (assignment detected); neither → Phase A flat

Run automatically by the scheduler (after exits, before entries) and on
demand from the dashboard Positions page.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import structlog

import src.logger as _log_setup  # noqa: F401
from src.positions import Position
from src.state_store import (
    get_strategy_state,
    get_ticker_state,
    set_strategy_state,
    set_ticker_state,
)

logger = structlog.get_logger(__name__)


def _positions_by_underlying(positions: list[Position]) -> dict[str, list[Position]]:
    grouped: dict[str, list[Position]] = {}
    for p in positions:
        grouped.setdefault(p.underlying, []).append(p)
    return grouped


def _sync_rsi2(
    ticker: str,
    held: list[Position],
    state_base: Path,
) -> dict[str, Any] | None:
    current = get_strategy_state("rsi2", ticker, state_base)
    equity_longs = [p for p in held if p.asset_class == "equity" and p.side == "long"]

    if equity_longs:
        pos = equity_longs[0]
        if current.get("in_position") and current.get("shares") == int(pos.qty):
            return None  # already in sync
        new_state = {
            "in_position": True,
            "shares": int(pos.qty),
            "entry_price": pos.avg_entry_price,
            # keep the original entry_date if we already had one
            "entry_date": current.get("entry_date") or date.today().isoformat(),
        }
    else:
        if not current.get("in_position"):
            return None
        new_state = {"in_position": False, "shares": 0,
                     "entry_price": None, "entry_date": None}

    set_strategy_state("rsi2", ticker, new_state, state_base)
    return {"strategy": "rsi2", "ticker": ticker, "state": new_state}


def _sync_pcs(
    ticker: str,
    held: list[Position],
    state_base: Path,
) -> dict[str, Any] | None:
    current = get_strategy_state("put_credit_spread", ticker, state_base)
    short_puts = [p for p in held if p.option_type == "put" and p.side == "short"]
    long_puts  = [p for p in held if p.option_type == "put" and p.side == "long"]

    if short_puts and long_puts:
        sp, lp = short_puts[0], long_puts[0]
        if current.get("open_spread") and current.get("short_strike") == sp.strike:
            return None
        new_state = {
            "open_spread": True,
            "short_strike": sp.strike,
            "long_strike": lp.strike,
            "expiry": sp.expiry,
            "entry_credit": current.get("entry_credit") or sp.avg_entry_price,
            "entry_date": current.get("entry_date") or date.today().isoformat(),
        }
    else:
        if not current.get("open_spread"):
            return None
        new_state = {"open_spread": False, "short_strike": None, "long_strike": None,
                     "expiry": None, "entry_credit": None, "entry_date": None}

    set_strategy_state("put_credit_spread", ticker, new_state, state_base)
    return {"strategy": "put_credit_spread", "ticker": ticker, "state": new_state}


def _sync_wheel(
    ticker: str,
    held: list[Position],
    wheel_state_path: str | Path,
) -> dict[str, Any] | None:
    current = get_ticker_state(wheel_state_path, ticker)
    short_puts   = [p for p in held if p.option_type == "put" and p.side == "short"]
    equity_longs = [p for p in held if p.asset_class == "equity" and p.side == "long"]
    shares = int(equity_longs[0].qty) if equity_longs else 0

    if shares >= 100:
        # Assignment detected (or shares bought) → Phase B covered-call mode
        new_state = {
            "phase": "B",
            "shares_held": shares,
            "cost_basis": current.get("cost_basis") or equity_longs[0].avg_entry_price,
            "assignment_date": current.get("assignment_date") or date.today().isoformat(),
            "open_put_strike": None,
            "open_put_expiry": None,
        }
    elif short_puts:
        sp = short_puts[0]
        new_state = {
            "phase": "A",
            "shares_held": 0,
            "cost_basis": None,
            "assignment_date": None,
            "open_put_strike": sp.strike,
            "open_put_expiry": sp.expiry,
        }
    else:
        new_state = {
            "phase": "A", "shares_held": 0, "cost_basis": None,
            "assignment_date": None, "open_put_strike": None, "open_put_expiry": None,
        }

    # Only write when something material changed
    material = ("phase", "shares_held", "open_put_strike", "open_put_expiry")
    if current and all(current.get(k) == new_state[k] for k in material):
        return None
    if not current and new_state["phase"] == "A" and not short_puts:
        return None  # never tracked and nothing held — don't create noise entries

    set_ticker_state(wheel_state_path, ticker, new_state)
    return {"strategy": "wheel", "ticker": ticker, "state": new_state}


def sync_state_from_positions(
    positions: list[Position],
    router_cfg: dict[str, Any],
    wheel_state_path: str | Path = "wheel_state.json",
    state_base: Path = Path("state"),
) -> list[dict[str, Any]]:
    """Reconcile every routed ticker's strategy state with live positions.

    Returns the list of changes applied (empty when everything was in sync).
    """
    assignments: dict[str, str] = router_cfg.get("router", {}).get("assignments", {})
    default = router_cfg.get("router", {}).get("default_strategy", "wheel")
    by_underlying = _positions_by_underlying(positions)

    # Every routed ticker AND every ticker we actually hold something in
    tickers = set(assignments) | set(by_underlying)

    changes: list[dict[str, Any]] = []
    for ticker in sorted(tickers):
        strategy = assignments.get(ticker, default)
        held = by_underlying.get(ticker, [])
        try:
            if strategy == "rsi2":
                change = _sync_rsi2(ticker, held, state_base)
            elif strategy == "put_credit_spread":
                change = _sync_pcs(ticker, held, state_base)
            elif strategy == "wheel":
                change = _sync_wheel(ticker, held, wheel_state_path)
            else:
                change = None
        except Exception as exc:
            logger.error("reconcile.ticker_failed", ticker=ticker, strategy=strategy, error=str(exc))
            continue
        if change:
            changes.append(change)
            logger.info("reconcile.state_synced", ticker=ticker, strategy=strategy,
                        new_state=change["state"])

    logger.info("reconcile.complete", tickers_checked=len(tickers), changes=len(changes))
    return changes


def run_reconciliation(executor, router_cfg: dict[str, Any], **kwargs) -> list[dict[str, Any]]:
    """Fetch live positions and sync all strategy state. Scheduler entry point."""
    from src.positions import fetch_positions

    positions = fetch_positions(executor, router_cfg)
    return sync_state_from_positions(positions, router_cfg, **kwargs)
