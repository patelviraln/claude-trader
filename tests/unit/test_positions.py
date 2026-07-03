"""Unit tests for src/positions.py and src/position_pnl.py (Phase 8)."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.position_pnl import (
    compute_dte,
    compute_pct_max_profit,
    enrich_position,
    mark_position,
)
from src.positions import (
    Position,
    append_position_event,
    fetch_positions,
    load_position_events,
    parse_occ_symbol,
    position_from_raw,
)


# ---------------------------------------------------------------------------
# OCC parsing
# ---------------------------------------------------------------------------

class TestParseOccSymbol:
    def test_parses_put(self):
        r = parse_occ_symbol("NVDA260626P00840000")
        assert r == {
            "underlying": "NVDA",
            "expiry": date(2026, 6, 26),
            "option_type": "put",
            "strike": 840.0,
        }

    def test_parses_call_fractional_strike(self):
        r = parse_occ_symbol("SPY260320C00512500")
        assert r["option_type"] == "call"
        assert r["strike"] == 512.5

    def test_equity_symbol_returns_none(self):
        assert parse_occ_symbol("TLT") is None
        assert parse_occ_symbol("BRK") is None

    def test_garbage_returns_none(self):
        assert parse_occ_symbol("NVDA26066XP00840000") is None
        assert parse_occ_symbol("nvda260626P00840000") is None


# ---------------------------------------------------------------------------
# Raw → Position mapping
# ---------------------------------------------------------------------------

def _raw_short_put(**overrides) -> dict:
    raw = {
        "symbol": "NVDA260626P00840000",
        "asset_class": "us_option",
        "qty": "-1",
        "side": "PositionSide.SHORT",
        "avg_entry_price": "7.50",
        "current_price": "3.20",
        "market_value": "-320.0",
        "unrealized_pl": "430.0",
        "unrealized_plpc": "0.5733",
    }
    raw.update(overrides)
    return raw


class TestPositionFromRaw:
    def test_maps_short_option(self):
        p = position_from_raw(_raw_short_put())
        assert p.asset_class == "option"
        assert p.side == "short"
        assert p.qty == 1.0
        assert p.underlying == "NVDA"
        assert p.strike == 840.0
        assert p.expiry == "2026-06-26"
        assert p.unrealized_pnl == 430.0

    def test_maps_equity_long(self):
        p = position_from_raw({
            "symbol": "TLT", "asset_class": "us_equity", "qty": "12",
            "side": "PositionSide.LONG", "avg_entry_price": "103.91",
            "current_price": "105.10", "market_value": "1261.2",
            "unrealized_pl": "14.28", "unrealized_plpc": "0.0114",
        })
        assert p.asset_class == "equity"
        assert p.side == "long"
        assert p.underlying == "TLT"
        assert p.strike is None

    def test_resolves_strategy_via_router(self):
        router_cfg = {"router": {"default_strategy": "wheel",
                                 "assignments": {"NVDA": "wheel", "TLT": "rsi2"}}}
        p = position_from_raw(_raw_short_put(), router_cfg)
        assert p.strategy_name == "wheel"

    def test_handles_missing_marks(self):
        p = position_from_raw(_raw_short_put(current_price=None, unrealized_pl=None))
        assert p.current_price is None
        assert p.unrealized_pnl is None


# ---------------------------------------------------------------------------
# P&L derivations
# ---------------------------------------------------------------------------

class TestPnlDerivations:
    def test_dte(self):
        expiry = (date.today() + timedelta(days=30)).isoformat()
        assert compute_dte(expiry) == 30
        assert compute_dte(None) is None

    def test_pct_max_profit_short(self):
        # sold at 7.50, now 3.20 → 57.3% of premium captured
        assert compute_pct_max_profit(7.50, 3.20, "short") == 57.3

    def test_pct_max_profit_losing_trade_negative(self):
        assert compute_pct_max_profit(7.50, 15.00, "short") == -100.0

    def test_pct_max_profit_none_for_long_or_unmarked(self):
        assert compute_pct_max_profit(7.50, 3.20, "long") is None
        assert compute_pct_max_profit(7.50, None, "short") is None

    def test_enrich_position(self):
        expiry = (date.today() + timedelta(days=40)).isoformat()
        p = Position(symbol="X260626P00100000", underlying="X", asset_class="option",
                     side="short", qty=1, avg_entry_price=4.0, current_price=1.0,
                     expiry=expiry)
        e = enrich_position(p)
        assert e.dte == 40
        assert e.pct_max_profit == 75.0

    def test_mark_position_fills_missing_option_mark(self):
        p = Position(symbol="NVDA260626P00840000", underlying="NVDA",
                     asset_class="option", side="short", qty=1,
                     avg_entry_price=7.50, current_price=None)
        adapter = MagicMock()
        adapter.get_option_quote.return_value = {"bid": 3.0, "ask": 3.4}
        marked = mark_position(p, adapter)
        assert marked.current_price == 3.2
        assert marked.unrealized_pnl == pytest.approx((7.50 - 3.2) * 100, abs=0.01)

    def test_mark_position_keeps_existing_mark(self):
        p = Position(symbol="TLT", underlying="TLT", asset_class="equity",
                     side="long", qty=10, avg_entry_price=100.0, current_price=101.0)
        adapter = MagicMock()
        assert mark_position(p, adapter).current_price == 101.0
        adapter.get_quote.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_positions + event log
# ---------------------------------------------------------------------------

class TestFetchPositions:
    def test_fetch_normalizes_and_enriches(self):
        executor = MagicMock()
        executor.get_positions.return_value = [_raw_short_put()]
        positions = fetch_positions(executor)
        assert len(positions) == 1
        assert positions[0].pct_max_profit == 57.3


class TestEventLog:
    def test_append_and_load_roundtrip(self, tmp_path):
        log = tmp_path / "positions.jsonl"
        append_position_event({"event": "manual_close_submitted", "symbol": "TLT",
                               "reason": "manual"}, path=log)
        append_position_event({"event": "exit_close_submitted", "symbol": "NVDA260626P00840000",
                               "reason": "profit_target"}, path=log)
        events = load_position_events(log)
        assert len(events) == 2
        assert events[0]["reason"] == "profit_target"  # newest first
        assert "ts" in events[0]

    def test_load_missing_file(self, tmp_path):
        assert load_position_events(tmp_path / "nope.jsonl") == []
