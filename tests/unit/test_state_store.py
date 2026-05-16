"""Unit tests for state_store helpers."""
import json

import pytest

from src.state_store import (
    get_ticker_state,
    read_state,
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
