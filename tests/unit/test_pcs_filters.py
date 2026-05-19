"""Unit tests for Put Credit Spread filter classes."""
from __future__ import annotations

import datetime

import pandas as pd
import pytest

from src.strategies.base import FilterResult
from src.strategies.spreads.filters import (
    IVRankGateFilter,
    SpreadLegBuilderFilter,
    SpreadOpenGateFilter,
)
from src.strategies.spreads.state import PCSState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_options(underlying: float = 100.0, dte: int = 37) -> pd.DataFrame:
    """Minimal options chain with realistic put deltas and varying premiums for spread building."""
    expiry = (datetime.date(2026, 5, 16) + datetime.timedelta(days=dte)).isoformat()
    rows = []
    # Higher strikes have higher put premiums — critical so net_credit > 0
    put_data = [
        (75,  -0.05, 0.05, 0.10),
        (80,  -0.10, 0.15, 0.25),
        (85,  -0.15, 0.40, 0.60),  # long leg candidate (~0.15 delta)
        (90,  -0.20, 0.70, 0.90),
        (95,  -0.30, 1.20, 1.40),  # short leg candidate (~0.30 delta)
        (100, -0.50, 2.50, 2.80),
        (105, -0.70, 4.00, 4.30),
        (110, -0.85, 6.00, 6.40),
    ]
    for strike, put_delta, bid, ask in put_data:
        rows.append({
            "strike": float(strike), "expiry": expiry, "option_type": "put",
            "delta": put_delta, "iv": 0.30, "bid": bid, "ask": ask,
            "volume": 500, "dte": dte,
        })
    for strike, call_delta in [(95, 0.70), (100, 0.50), (105, 0.30)]:
        rows.append({
            "strike": float(strike), "expiry": expiry, "option_type": "call",
            "delta": call_delta, "iv": 0.28, "bid": 0.8, "ask": 1.0,
            "volume": 300, "dte": dte,
        })
    return pd.DataFrame(rows)


_OPEN_STATE = PCSState(
    open_spread=True,
    short_strike=95.0,
    long_strike=90.0,
    expiry="2026-06-22",
    entry_credit=0.80,
    entry_date="2026-05-16",
)
_CLOSED_STATE = PCSState.default()


# ---------------------------------------------------------------------------
# SpreadOpenGateFilter
# ---------------------------------------------------------------------------

class TestSpreadOpenGateFilter:
    def test_no_open_spread_passes(self):
        f = SpreadOpenGateFilter()
        r = f.run({}, _CLOSED_STATE, {})
        assert r.passed
        assert not r.hard_stop

    def test_open_spread_hard_stops(self):
        f = SpreadOpenGateFilter()
        r = f.run({}, _OPEN_STATE, {})
        assert not r.passed
        assert r.hard_stop
        assert "already open" in r.reason

    def test_returns_filter_result_type(self):
        f = SpreadOpenGateFilter()
        r = f.run({}, _CLOSED_STATE, {})
        assert isinstance(r, FilterResult)

    def test_correct_filter_name(self):
        f = SpreadOpenGateFilter()
        r = f.run({}, _CLOSED_STATE, {})
        assert r.filter_name == "spread_open_gate"


# ---------------------------------------------------------------------------
# IVRankGateFilter
# ---------------------------------------------------------------------------

class TestIVRankGateFilter:
    def test_adequate_iv_rank_passes(self):
        f = IVRankGateFilter(min_iv_rank=25.0)
        r = f.run({"iv_rank": 40.0}, _CLOSED_STATE, {})
        assert r.passed
        assert not r.hard_stop

    def test_exactly_at_threshold_passes(self):
        f = IVRankGateFilter(min_iv_rank=25.0)
        r = f.run({"iv_rank": 25.0}, _CLOSED_STATE, {})
        assert r.passed

    def test_low_iv_rank_hard_stops(self):
        f = IVRankGateFilter(min_iv_rank=25.0)
        r = f.run({"iv_rank": 10.0}, _CLOSED_STATE, {})
        assert not r.passed
        assert r.hard_stop
        assert "10.0" in r.reason

    def test_none_iv_rank_soft_flag(self):
        f = IVRankGateFilter(min_iv_rank=25.0)
        r = f.run({"iv_rank": None}, _CLOSED_STATE, {})
        assert r.passed
        assert not r.hard_stop
        assert r.adjustments.get("soft_flag") == "iv_rank_missing"

    def test_missing_key_soft_flag(self):
        f = IVRankGateFilter(min_iv_rank=25.0)
        r = f.run({}, _CLOSED_STATE, {})
        assert r.passed
        assert r.adjustments.get("soft_flag") == "iv_rank_missing"

    def test_adjustments_contain_iv_rank_when_passing(self):
        f = IVRankGateFilter(min_iv_rank=25.0)
        r = f.run({"iv_rank": 50.0}, _CLOSED_STATE, {})
        assert r.adjustments.get("iv_rank") == 50.0


# ---------------------------------------------------------------------------
# SpreadLegBuilderFilter
# ---------------------------------------------------------------------------

class TestSpreadLegBuilderFilter:
    def test_builds_valid_spread(self):
        f = SpreadLegBuilderFilter(
            short_delta_target=0.30, short_delta_tolerance=0.05,
            long_delta_target=0.15, long_delta_tolerance=0.07,
            min_credit=0.10, max_spread_width=20.0,
        )
        chain = _make_options()
        r = f.run({"options_chain": chain}, _CLOSED_STATE, {})
        assert r.passed
        assert not r.hard_stop

    def test_adjustments_contain_required_keys(self):
        f = SpreadLegBuilderFilter(
            short_delta_target=0.30, short_delta_tolerance=0.05,
            long_delta_target=0.15, long_delta_tolerance=0.07,
            min_credit=0.10, max_spread_width=20.0,
        )
        r = f.run({"options_chain": _make_options()}, _CLOSED_STATE, {})
        assert r.passed
        for key in ("short_strike", "long_strike", "net_credit", "spread_width",
                    "max_profit", "max_loss", "delta_estimate"):
            assert key in r.adjustments, f"Missing key: {key}"

    def test_short_strike_above_long_strike(self):
        f = SpreadLegBuilderFilter(
            short_delta_target=0.30, short_delta_tolerance=0.05,
            long_delta_target=0.15, long_delta_tolerance=0.07,
            min_credit=0.10, max_spread_width=20.0,
        )
        r = f.run({"options_chain": _make_options()}, _CLOSED_STATE, {})
        assert r.adjustments["short_strike"] > r.adjustments["long_strike"]

    def test_credit_below_minimum_hard_stops(self):
        f = SpreadLegBuilderFilter(
            short_delta_target=0.30, short_delta_tolerance=0.05,
            long_delta_target=0.15, long_delta_tolerance=0.07,
            min_credit=5.00,  # impossibly high
            max_spread_width=20.0,
        )
        r = f.run({"options_chain": _make_options()}, _CLOSED_STATE, {})
        assert not r.passed
        assert r.hard_stop
        assert "credit" in r.reason.lower()

    def test_empty_chain_hard_stops(self):
        f = SpreadLegBuilderFilter()
        r = f.run({"options_chain": pd.DataFrame()}, _CLOSED_STATE, {})
        assert not r.passed
        assert r.hard_stop

    def test_no_puts_hard_stops(self):
        f = SpreadLegBuilderFilter()
        calls_only = pd.DataFrame([{
            "strike": 100.0, "expiry": "2026-06-22", "option_type": "call",
            "delta": 0.50, "iv": 0.30, "bid": 1.0, "ask": 1.2, "volume": 100, "dte": 37,
        }])
        r = f.run({"options_chain": calls_only}, _CLOSED_STATE, {})
        assert not r.passed
        assert r.hard_stop

    def test_max_profit_equals_credit_times_100(self):
        f = SpreadLegBuilderFilter(
            short_delta_target=0.30, short_delta_tolerance=0.05,
            long_delta_target=0.15, long_delta_tolerance=0.07,
            min_credit=0.10, max_spread_width=20.0,
        )
        r = f.run({"options_chain": _make_options()}, _CLOSED_STATE, {})
        assert r.passed
        adj = r.adjustments
        assert abs(adj["max_profit"] - adj["net_credit"] * 100) < 0.01

    def test_spread_width_exceeds_max_hard_stops(self):
        f = SpreadLegBuilderFilter(
            short_delta_target=0.30, short_delta_tolerance=0.05,
            long_delta_target=0.15, long_delta_tolerance=0.07,
            min_credit=0.10, max_spread_width=1.0,  # narrower than any real spread
        )
        # With strikes at 95 and 85, width = 10 > 1.0 → hard stop
        r = f.run({"options_chain": _make_options()}, _CLOSED_STATE, {})
        assert not r.passed
        assert r.hard_stop

    def test_correct_filter_name(self):
        f = SpreadLegBuilderFilter()
        r = f.run({"options_chain": pd.DataFrame()}, _CLOSED_STATE, {})
        assert r.filter_name == "spread_leg_builder"
