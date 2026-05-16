"""Unit tests for src/dashboard_data.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dashboard_data import (
    get_all_ticker_states,
    get_all_tracked_tickers,
    get_iv_series,
    get_last_signal,
    get_recent_signals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state), encoding="utf-8")


def _write_signals(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _sig(ticker="AAPL", phase="SELL_PUT", ts="2026-05-16T10:00:00Z") -> dict:
    return {
        "ticker": ticker, "phase": phase,
        "signal_timestamp": ts,
        "underlying_price": 200.0,
        "recommended_strike": 190.0,
        "iv_rank": 55.0,
        "option_mid": 3.5,
        "confidence_tier": "HIGH",
        "order_id": None,
    }


# ---------------------------------------------------------------------------
# get_all_ticker_states
# ---------------------------------------------------------------------------

class TestGetAllTickerStates:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assert get_all_ticker_states(tmp_path / "missing.json") == {}

    def test_returns_all_tickers(self, tmp_path):
        p = tmp_path / "state.json"
        _write_state(p, {"AAPL": {"phase": "A"}, "NVDA": {"phase": "B"}})
        result = get_all_ticker_states(p)
        assert set(result.keys()) == {"AAPL", "NVDA"}

    def test_preserves_state_fields(self, tmp_path):
        p = tmp_path / "state.json"
        _write_state(p, {"MSFT": {"phase": "B", "shares_held": 100, "cost_basis": 400.0}})
        assert get_all_ticker_states(p)["MSFT"]["cost_basis"] == 400.0


# ---------------------------------------------------------------------------
# get_recent_signals
# ---------------------------------------------------------------------------

class TestGetRecentSignals:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assert get_recent_signals(tmp_path / "missing.jsonl") == []

    def test_returns_newest_first(self, tmp_path):
        p = tmp_path / "signals.jsonl"
        _write_signals(p, [_sig(ts="2026-05-14T10:00:00Z"), _sig(ts="2026-05-16T10:00:00Z")])
        result = get_recent_signals(p)
        assert result[0]["signal_timestamp"] == "2026-05-16T10:00:00Z"

    def test_filters_by_ticker(self, tmp_path):
        p = tmp_path / "signals.jsonl"
        _write_signals(p, [_sig("AAPL"), _sig("NVDA"), _sig("AAPL")])
        result = get_recent_signals(p, ticker="AAPL")
        assert all(r["ticker"] == "AAPL" for r in result)
        assert len(result) == 2

    def test_respects_n_limit(self, tmp_path):
        p = tmp_path / "signals.jsonl"
        _write_signals(p, [_sig() for _ in range(10)])
        assert len(get_recent_signals(p, n=3)) == 3

    def test_skips_blank_and_malformed_lines(self, tmp_path):
        p = tmp_path / "signals.jsonl"
        p.write_text('{"ticker":"AAPL","phase":"SELL_PUT","signal_timestamp":"2026-05-16T10:00:00Z","underlying_price":200.0}\n\nnot-json\n', encoding="utf-8")
        result = get_recent_signals(p)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# get_iv_series
# ---------------------------------------------------------------------------

class TestGetIvSeries:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assert get_iv_series("AAPL", tmp_path) == []

    def test_returns_entries_in_order(self, tmp_path):
        (tmp_path / "AAPL.jsonl").write_text(
            '{"date":"2026-05-14","iv":0.30}\n{"date":"2026-05-15","iv":0.35}\n',
            encoding="utf-8",
        )
        result = get_iv_series("AAPL", tmp_path)
        assert result == [{"date": "2026-05-14", "iv": 0.30}, {"date": "2026-05-15", "iv": 0.35}]

    def test_ticker_case_insensitive(self, tmp_path):
        (tmp_path / "MSFT.jsonl").write_text('{"date":"2026-05-16","iv":0.22}\n', encoding="utf-8")
        assert get_iv_series("msft", tmp_path) == [{"date": "2026-05-16", "iv": 0.22}]

    def test_skips_entries_missing_iv(self, tmp_path):
        (tmp_path / "NVDA.jsonl").write_text(
            '{"date":"2026-05-14"}\n{"date":"2026-05-15","iv":0.45}\n', encoding="utf-8"
        )
        assert get_iv_series("NVDA", tmp_path) == [{"date": "2026-05-15", "iv": 0.45}]


# ---------------------------------------------------------------------------
# get_last_signal
# ---------------------------------------------------------------------------

class TestGetLastSignal:
    def test_returns_none_when_no_signals(self, tmp_path):
        assert get_last_signal("AAPL", tmp_path / "missing.jsonl") is None

    def test_returns_most_recent_signal(self, tmp_path):
        p = tmp_path / "signals.jsonl"
        _write_signals(p, [_sig("AAPL", ts="2026-05-14T10:00:00Z"), _sig("AAPL", ts="2026-05-16T10:00:00Z")])
        result = get_last_signal("AAPL", p)
        assert result["signal_timestamp"] == "2026-05-16T10:00:00Z"


# ---------------------------------------------------------------------------
# get_all_tracked_tickers
# ---------------------------------------------------------------------------

class TestGetAllTrackedTickers:
    def test_returns_sorted_union(self, tmp_path):
        state_path = tmp_path / "state.json"
        signals_path = tmp_path / "signals.jsonl"
        _write_state(state_path, {"MSFT": {"phase": "A"}, "NVDA": {"phase": "B"}})
        _write_signals(signals_path, [_sig("AAPL"), _sig("MSFT")])
        result = get_all_tracked_tickers(state_path, signals_path)
        assert "AAPL" in result
        assert "MSFT" in result
        assert "NVDA" in result
        assert result == sorted(result)

    def test_excludes_fixture_tickers(self, tmp_path):
        state_path = tmp_path / "state.json"
        signals_path = tmp_path / "signals.jsonl"
        _write_state(state_path, {"WXYZ": {"phase": "A"}, "AAPL": {"phase": "A"}})
        _write_signals(signals_path, [_sig("AAPL")])
        result = get_all_tracked_tickers(state_path, signals_path)
        assert "WXYZ" not in result
        assert "AAPL" in result
