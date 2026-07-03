"""
Phase 8 — open-position tracking.

Reads live positions from the Alpaca paper account (via AlpacaOrderExecutor's
TradingClient), parses OCC option symbols, resolves each position's strategy
through the router, and appends lifecycle events to positions.jsonl.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401

logger = structlog.get_logger(__name__)

POSITIONS_LOG_PATH = Path("positions.jsonl")

_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def parse_occ_symbol(symbol: str) -> dict[str, Any] | None:
    """Parse an OCC option symbol (e.g. NVDA260626P00840000).

    Returns {underlying, expiry (date), option_type, strike} or None when the
    symbol is not OCC-shaped (plain equity tickers return None).
    """
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    underlying, date_str, type_char, strike_str = m.groups()
    try:
        expiry = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6]))
    except ValueError:
        return None
    return {
        "underlying": underlying,
        "expiry": expiry,
        "option_type": "call" if type_char == "C" else "put",
        "strike": int(strike_str) / 1000.0,
    }


class Position(BaseModel):
    """One open position, normalized from Alpaca's account state."""
    symbol: str                    # OCC symbol for options, ticker for equity
    underlying: str
    asset_class: str               # "option" | "equity"
    side: str                      # "long" | "short"
    qty: float                     # absolute contracts/shares
    avg_entry_price: float         # per share / per contract-share
    current_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_plpc: float | None = None   # fraction, e.g. 0.12 = +12%
    strategy_name: str | None = None
    # Option-specific (None for equity):
    strike: float | None = None
    expiry: str | None = None      # YYYY-MM-DD
    option_type: str | None = None
    dte: int | None = None
    pct_max_profit: float | None = None    # short options: % of premium captured


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def position_from_raw(raw: dict[str, Any], router_cfg: dict | None = None) -> Position:
    """Build a Position from a raw Alpaca position dict (see executor.get_positions)."""
    symbol = raw["symbol"]
    occ = parse_occ_symbol(symbol)
    is_option = occ is not None or raw.get("asset_class") == "us_option"
    underlying = occ["underlying"] if occ else symbol

    strategy_name = None
    if router_cfg:
        from src.router import resolve_strategy
        strategy_name = resolve_strategy(underlying, router_cfg)

    qty = abs(_to_float(raw.get("qty")) or 0.0)
    side = str(raw.get("side", "long")).replace("PositionSide.", "").lower()
    if side not in ("long", "short"):
        side = "short" if (_to_float(raw.get("qty")) or 0.0) < 0 else "long"

    return Position(
        symbol=symbol,
        underlying=underlying,
        asset_class="option" if is_option else "equity",
        side=side,
        qty=qty,
        avg_entry_price=_to_float(raw.get("avg_entry_price")) or 0.0,
        current_price=_to_float(raw.get("current_price")),
        market_value=_to_float(raw.get("market_value")),
        unrealized_pnl=_to_float(raw.get("unrealized_pl")),
        unrealized_plpc=_to_float(raw.get("unrealized_plpc")),
        strategy_name=strategy_name,
        strike=occ["strike"] if occ else None,
        expiry=occ["expiry"].isoformat() if occ else None,
        option_type=occ["option_type"] if occ else None,
    )


def fetch_positions(executor, router_cfg: dict | None = None) -> list[Position]:
    """Fetch all open positions from the trading account, normalized + enriched."""
    from src.position_pnl import enrich_position

    raw_positions = executor.get_positions()
    positions = [position_from_raw(r, router_cfg) for r in raw_positions]
    enriched = [enrich_position(p) for p in positions]
    logger.info("positions_fetched", count=len(enriched))
    return enriched


# ---------------------------------------------------------------------------
# positions.jsonl event log — every fill/close event with reason
# ---------------------------------------------------------------------------


def append_position_event(event: dict[str, Any], path: str | Path = POSITIONS_LOG_PATH) -> None:
    """Append one lifecycle event (open/close/exit) to the positions log."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    # structlog reserves the "event" kwarg for the log message itself
    log_fields = {("event_type" if k == "event" else k): v
                  for k, v in record.items() if k != "ts"}
    logger.info("position_event", **log_fields)


def load_position_events(path: str | Path = POSITIONS_LOG_PATH, n: int = 100) -> list[dict]:
    """Return the last n position events, newest first."""
    p = Path(path)
    if not p.exists():
        return []
    events = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(events))[:n]
