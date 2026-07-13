"""Unit tests for src/reconcile.py — fill-to-state reconciliation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.positions import Position
from src.reconcile import sync_state_from_positions
from src.state_store import get_strategy_state, get_ticker_state

ROUTER_CFG = {
    "router": {
        "default_strategy": "wheel",
        "assignments": {"NVDA": "wheel", "SPY": "put_credit_spread", "TLT": "rsi2",
                        "AMD": "red_day_csp"},
    }
}


def _equity(ticker: str, qty: float, entry: float) -> Position:
    return Position(symbol=ticker, underlying=ticker, asset_class="equity",
                    side="long", qty=qty, avg_entry_price=entry)


def _option(underlying: str, side: str, strike: float, expiry: str,
            option_type: str = "put", entry: float = 5.0) -> Position:
    return Position(symbol=f"{underlying}...", underlying=underlying,
                    asset_class="option", side=side, qty=1,
                    avg_entry_price=entry, strike=strike, expiry=expiry,
                    option_type=option_type)


@pytest.fixture()
def paths(tmp_path):
    return {"wheel": tmp_path / "wheel_state.json", "base": tmp_path / "state"}


class TestRsi2Sync:
    def test_fill_flips_state_to_in_position(self, paths):
        changes = sync_state_from_positions(
            [_equity("TLT", 12, 103.91)], ROUTER_CFG,
            wheel_state_path=paths["wheel"], state_base=paths["base"])
        assert any(c["ticker"] == "TLT" for c in changes)
        state = get_strategy_state("rsi2", "TLT", paths["base"])
        assert state["in_position"] is True
        assert state["shares"] == 12
        assert state["entry_price"] == 103.91

    def test_close_flips_state_to_flat(self, paths):
        sync_state_from_positions([_equity("TLT", 12, 103.91)], ROUTER_CFG,
                                  wheel_state_path=paths["wheel"], state_base=paths["base"])
        changes = sync_state_from_positions([], ROUTER_CFG,
                                            wheel_state_path=paths["wheel"],
                                            state_base=paths["base"])
        state = get_strategy_state("rsi2", "TLT", paths["base"])
        assert state["in_position"] is False
        assert state["shares"] == 0
        assert any(c["ticker"] == "TLT" for c in changes)

    def test_in_sync_state_untouched(self, paths):
        pos = [_equity("TLT", 12, 103.91)]
        sync_state_from_positions(pos, ROUTER_CFG,
                                  wheel_state_path=paths["wheel"], state_base=paths["base"])
        changes = sync_state_from_positions(pos, ROUTER_CFG,
                                            wheel_state_path=paths["wheel"],
                                            state_base=paths["base"])
        assert changes == []

    def test_entry_date_preserved_on_resync(self, paths):
        from src.state_store import set_strategy_state
        set_strategy_state("rsi2", "TLT",
                           {"in_position": True, "shares": 10, "entry_price": 100.0,
                            "entry_date": "2026-06-25"}, paths["base"])
        sync_state_from_positions([_equity("TLT", 12, 103.91)], ROUTER_CFG,
                                  wheel_state_path=paths["wheel"], state_base=paths["base"])
        state = get_strategy_state("rsi2", "TLT", paths["base"])
        assert state["entry_date"] == "2026-06-25"  # original entry kept
        assert state["shares"] == 12                # qty corrected


class TestWheelSync:
    def test_short_put_recorded(self, paths):
        changes = sync_state_from_positions(
            [_option("NVDA", "short", 840.0, "2026-08-21", entry=7.5)], ROUTER_CFG,
            wheel_state_path=paths["wheel"], state_base=paths["base"])
        state = get_ticker_state(paths["wheel"], "NVDA")
        assert state["open_put_strike"] == 840.0
        assert state["open_put_expiry"] == "2026-08-21"
        assert state["phase"] == "A"
        assert any(c["ticker"] == "NVDA" for c in changes)

    def test_assignment_detected_100_shares(self, paths):
        sync_state_from_positions([_equity("NVDA", 100, 840.0)], ROUTER_CFG,
                                  wheel_state_path=paths["wheel"], state_base=paths["base"])
        state = get_ticker_state(paths["wheel"], "NVDA")
        assert state["phase"] == "B"
        assert state["shares_held"] == 100
        assert state["cost_basis"] == 840.0
        assert state["open_put_strike"] is None

    def test_put_expired_clears_open_put(self, paths):
        sync_state_from_positions([_option("NVDA", "short", 840.0, "2026-08-21")],
                                  ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        sync_state_from_positions([], ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        state = get_ticker_state(paths["wheel"], "NVDA")
        assert state["open_put_strike"] is None
        assert state["phase"] == "A"

    def test_untracked_flat_ticker_not_created(self, paths):
        sync_state_from_positions([], ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        assert get_ticker_state(paths["wheel"], "AAPL") == {}


class TestPcsSync:
    def test_spread_pair_recorded(self, paths):
        positions = [
            _option("SPY", "short", 500.0, "2026-08-21", entry=1.2),
            _option("SPY", "long", 490.0, "2026-08-21", entry=0.6),
        ]
        sync_state_from_positions(positions, ROUTER_CFG,
                                  wheel_state_path=paths["wheel"], state_base=paths["base"])
        state = get_strategy_state("put_credit_spread", "SPY", paths["base"])
        assert state["open_spread"] is True
        assert state["short_strike"] == 500.0
        assert state["long_strike"] == 490.0

    def test_spread_closed_clears_state(self, paths):
        positions = [
            _option("SPY", "short", 500.0, "2026-08-21"),
            _option("SPY", "long", 490.0, "2026-08-21"),
        ]
        sync_state_from_positions(positions, ROUTER_CFG,
                                  wheel_state_path=paths["wheel"], state_base=paths["base"])
        sync_state_from_positions([], ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        state = get_strategy_state("put_credit_spread", "SPY", paths["base"])
        assert state["open_spread"] is False


class TestRedDaySync:
    def test_short_put_recorded(self, paths):
        changes = sync_state_from_positions(
            [_option("AMD", "short", 155.0, "2026-08-21", entry=3.1)], ROUTER_CFG,
            wheel_state_path=paths["wheel"], state_base=paths["base"])
        state = get_strategy_state("red_day_csp", "AMD", paths["base"])
        assert state["open_put_strike"] == 155.0
        assert state["open_put_expiry"] == "2026-08-21"
        assert any(c["ticker"] == "AMD" for c in changes)

    def test_put_closed_clears_state(self, paths):
        sync_state_from_positions([_option("AMD", "short", 155.0, "2026-08-21")],
                                  ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        sync_state_from_positions([], ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        state = get_strategy_state("red_day_csp", "AMD", paths["base"])
        assert state["open_put_strike"] is None

    def test_in_sync_untouched(self, paths):
        pos = [_option("AMD", "short", 155.0, "2026-08-21")]
        sync_state_from_positions(pos, ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        changes = sync_state_from_positions(pos, ROUTER_CFG,
                                            wheel_state_path=paths["wheel"],
                                            state_base=paths["base"])
        assert changes == []

    def test_synced_state_blocks_reentry(self, paths):
        """Reconciled open put must trip the OpenPositionGateFilter."""
        from src.strategies.red_day.filters import OpenPositionGateFilter
        from src.strategies.red_day.state import RedDayState
        sync_state_from_positions([_option("AMD", "short", 155.0, "2026-08-21")],
                                  ROUTER_CFG, wheel_state_path=paths["wheel"],
                                  state_base=paths["base"])
        state = RedDayState.from_dict(
            get_strategy_state("red_day_csp", "AMD", paths["base"]))
        r = OpenPositionGateFilter().run({"ticker": "AMD"}, state, {})
        assert not r.passed and r.hard_stop


class TestDuplicateEntryGuard:
    def test_wheel_blocks_new_put_when_one_open(self):
        """After reconcile records an open put, the phase gate hard-stops."""
        from src.strategies.wheel.filters import PhaseGateFilter
        from src.strategies.wheel.state import WheelState

        state = WheelState(phase="A", open_put_strike=840.0,
                           open_put_expiry="2026-08-21")
        r = PhaseGateFilter().run({"ticker": "NVDA"}, state, {})
        assert r.passed is False
        assert r.hard_stop is True
        assert "Open put already active" in r.reason

    def test_wheel_allows_entry_when_flat(self):
        from src.strategies.wheel.filters import PhaseGateFilter
        from src.strategies.wheel.state import WheelState

        r = PhaseGateFilter().run({"ticker": "NVDA"}, WheelState(), {})
        assert r.passed is True
