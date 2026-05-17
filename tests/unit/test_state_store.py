"""Unit tests for state_store helpers."""
import json

import pytest

from src.state_store import (
    all_tickers,
    get_strategy_state,
    get_ticker_state,
    read_state,
    set_strategy_state,
    set_ticker_state,
    write_state,
)


def test_read_missing_file_returns_empty(tmp_path):
    p = tmp_path / "missing.json"
    assert read_state(p) == {}


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    data = {"AAPL": {"phase": "A", "shares_held": 0}}
    write_state(p, data)
    assert read_state(p) == data


def test_atomic_write_creates_file(tmp_path):
    p = tmp_path / "state.json"
    write_state(p, {"X": 1})
    assert p.exists()
    assert not (tmp_path / "state.tmp").exists()


def test_get_ticker_state_missing_ticker(tmp_path):
    p = tmp_path / "state.json"
    write_state(p, {"AAPL": {"phase": "A"}})
    assert get_ticker_state(p, "MSFT") == {}


def test_set_ticker_state_upsert(tmp_path):
    p = tmp_path / "state.json"
    write_state(p, {"AAPL": {"phase": "A"}})
    set_ticker_state(p, "MSFT", {"phase": "B"})
    state = read_state(p)
    assert "AAPL" in state  # original preserved
    assert state["MSFT"]["phase"] == "B"


# ---------------------------------------------------------------------------
# Per-strategy API (Phase A3)
# ---------------------------------------------------------------------------

def test_get_strategy_state_returns_empty_when_missing(tmp_path):
    result = get_strategy_state("wheel", "AAPL", base_path=tmp_path)
    assert result == {}


def test_set_and_get_strategy_state_roundtrip(tmp_path):
    ticker_state = {"phase": "A", "shares_held": 0}
    set_strategy_state("wheel", "AAPL", ticker_state, base_path=tmp_path)
    result = get_strategy_state("wheel", "AAPL", base_path=tmp_path)
    assert result == ticker_state


def test_strategy_state_stored_in_correct_file(tmp_path):
    set_strategy_state("put_credit_spread", "SPY", {"open_spread": False}, base_path=tmp_path)
    assert (tmp_path / "put_credit_spread.json").exists()
    assert not (tmp_path / "wheel.json").exists()


def test_strategy_state_preserves_other_tickers(tmp_path):
    set_strategy_state("wheel", "AAPL", {"phase": "A"}, base_path=tmp_path)
    set_strategy_state("wheel", "MSFT", {"phase": "B"}, base_path=tmp_path)
    assert get_strategy_state("wheel", "AAPL", base_path=tmp_path) == {"phase": "A"}
    assert get_strategy_state("wheel", "MSFT", base_path=tmp_path) == {"phase": "B"}


def test_all_tickers_returns_sorted_list(tmp_path):
    set_strategy_state("wheel", "NVDA", {}, base_path=tmp_path)
    set_strategy_state("wheel", "AAPL", {}, base_path=tmp_path)
    set_strategy_state("wheel", "MSFT", {}, base_path=tmp_path)
    assert all_tickers("wheel", base_path=tmp_path) == ["AAPL", "MSFT", "NVDA"]


def test_all_tickers_returns_empty_when_no_state(tmp_path):
    assert all_tickers("rsi2", base_path=tmp_path) == []
