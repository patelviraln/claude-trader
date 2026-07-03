"""
Phase 8 — mark-to-market and derived P&L metrics for open positions.

Alpaca already supplies current_price / unrealized_pl on each position; this
module fills the gaps (stale quotes via adapter.get_option_quote) and derives
the trade-management numbers the dashboard and exit engine care about:
days to expiry and, for short options, % of max profit captured.
"""
from __future__ import annotations

from datetime import date

from src.positions import Position


def compute_dte(expiry_iso: str | None, today: date | None = None) -> int | None:
    if not expiry_iso:
        return None
    today = today or date.today()
    return (date.fromisoformat(expiry_iso) - today).days


def compute_pct_max_profit(
    avg_entry_price: float,
    current_price: float | None,
    side: str,
) -> float | None:
    """For a short option: how much of the collected premium has decayed.

    100% = option now worthless (full premium kept); negative = losing trade.
    Long positions and unknown marks return None.
    """
    if side != "short" or current_price is None or avg_entry_price <= 0:
        return None
    return round((avg_entry_price - current_price) / avg_entry_price * 100, 1)


def mark_position(position: Position, adapter) -> Position:
    """Fill current_price / unrealized_pnl from a live quote when Alpaca's
    position snapshot came back without a mark (e.g. pre-market)."""
    if position.current_price is not None:
        return position

    mid = None
    if position.asset_class == "option":
        q = adapter.get_option_quote(position.symbol)
        bid, ask = q.get("bid") or 0.0, q.get("ask") or 0.0
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
    else:
        q = adapter.get_quote(position.symbol)
        mid = q.get("last") or None

    if mid is None:
        return position

    updated = position.model_copy(update={"current_price": round(mid, 4)})
    multiplier = 100 if position.asset_class == "option" else 1
    sign = -1 if position.side == "short" else 1
    updated.unrealized_pnl = round(
        sign * (mid - position.avg_entry_price) * position.qty * multiplier, 2
    )
    return updated


def enrich_position(position: Position, today: date | None = None) -> Position:
    """Attach derived metrics (dte, pct_max_profit) to a position."""
    return position.model_copy(update={
        "dte": compute_dte(position.expiry, today),
        "pct_max_profit": compute_pct_max_profit(
            position.avg_entry_price, position.current_price, position.side
        ),
    })
