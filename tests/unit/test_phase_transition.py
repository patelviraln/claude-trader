"""Unit tests for phase_transition.py — assign and exit commands."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_assign(tmp_path: Path, ticker: str, cost_basis: float, initial_state: dict | None = None) -> Path:
    state_path = tmp_path / "wheel_state.json"
    if initial_state is not None:
        _write_state(state_path, initial_state)

    import argparse
    from phase_transition import cmd_assign
    args = argparse.Namespace(
        state=str(state_path),
        ticker=ticker,
        cost_basis=cost_basis,
    )
    cmd_assign(args)
    return state_path


def _run_exit(tmp_path: Path, ticker: str, initial_state: dict | None = None) -> Path:
    state_path = tmp_path / "wheel_state.json"
    if initial_state is not None:
        _write_state(state_path, initial_state)

    import argparse
    from phase_transition import cmd_exit
    args = argparse.Namespace(
        state=str(state_path),
        ticker=ticker,
    )
    cmd_exit(args)
    return state_path


# ---------------------------------------------------------------------------
# cmd_assign tests
# ---------------------------------------------------------------------------

class TestCmdAssign:
    def test_moves_ticker_to_phase_b(self, tmp_path):
        state_path = _run_assign(
            tmp_path, "NVDA", 880.50,
            initial_state={"NVDA": {"phase": "A", "shares_held": 0, "cost_basis": None}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["phase"] == "B"

    def test_sets_100_shares(self, tmp_path):
        state_path = _run_assign(tmp_path, "NVDA", 880.50)
        state = _read_state(state_path)
        assert state["NVDA"]["shares_held"] == 100

    def test_sets_cost_basis(self, tmp_path):
        state_path = _run_assign(tmp_path, "NVDA", 880.50)
        state = _read_state(state_path)
        assert state["NVDA"]["cost_basis"] == pytest.approx(880.50)

    def test_rounds_cost_basis_to_4_decimals(self, tmp_path):
        state_path = _run_assign(tmp_path, "NVDA", 880.123456789)
        state = _read_state(state_path)
        assert state["NVDA"]["cost_basis"] == pytest.approx(880.1235)

    def test_sets_assignment_date(self, tmp_path):
        state_path = _run_assign(tmp_path, "NVDA", 880.50)
        state = _read_state(state_path)
        assert state["NVDA"]["assignment_date"] is not None
        assert len(state["NVDA"]["assignment_date"]) == 10  # YYYY-MM-DD

    def test_clears_open_put_fields(self, tmp_path):
        state_path = _run_assign(
            tmp_path, "NVDA", 880.50,
            initial_state={"NVDA": {"phase": "A", "open_put_strike": 850.0, "open_put_expiry": "2026-06-26"}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["open_put_strike"] is None
        assert state["NVDA"]["open_put_expiry"] is None

    def test_does_not_overwrite_other_tickers(self, tmp_path):
        state_path = _run_assign(
            tmp_path, "NVDA", 880.50,
            initial_state={"AAPL": {"phase": "B", "shares_held": 100, "cost_basis": 175.0}}
        )
        state = _read_state(state_path)
        assert state["AAPL"]["phase"] == "B"

    def test_no_op_when_already_phase_b(self, tmp_path, capsys):
        state_path = _run_assign(
            tmp_path, "NVDA", 880.50,
            initial_state={"NVDA": {"phase": "B", "shares_held": 100, "cost_basis": 870.0}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["cost_basis"] == pytest.approx(870.0)  # unchanged

    def test_normalizes_ticker_to_uppercase(self, tmp_path):
        state_path = _run_assign(tmp_path, "nvda", 880.50)
        state = _read_state(state_path)
        assert "NVDA" in state


# ---------------------------------------------------------------------------
# cmd_exit tests
# ---------------------------------------------------------------------------

class TestCmdExit:
    def test_moves_ticker_to_phase_a(self, tmp_path):
        state_path = _run_exit(
            tmp_path, "NVDA",
            initial_state={"NVDA": {"phase": "B", "shares_held": 100, "cost_basis": 880.50}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["phase"] == "A"

    def test_clears_shares_held(self, tmp_path):
        state_path = _run_exit(
            tmp_path, "NVDA",
            initial_state={"NVDA": {"phase": "B", "shares_held": 100, "cost_basis": 880.50}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["shares_held"] == 0

    def test_clears_cost_basis(self, tmp_path):
        state_path = _run_exit(
            tmp_path, "NVDA",
            initial_state={"NVDA": {"phase": "B", "shares_held": 100, "cost_basis": 880.50}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["cost_basis"] is None

    def test_clears_assignment_date(self, tmp_path):
        state_path = _run_exit(
            tmp_path, "NVDA",
            initial_state={"NVDA": {"phase": "B", "assignment_date": "2026-05-01"}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["assignment_date"] is None

    def test_does_not_overwrite_other_tickers(self, tmp_path):
        state_path = _run_exit(
            tmp_path, "NVDA",
            initial_state={
                "NVDA": {"phase": "B", "shares_held": 100, "cost_basis": 880.0},
                "AAPL": {"phase": "A", "shares_held": 0, "cost_basis": None},
            }
        )
        state = _read_state(state_path)
        assert state["AAPL"]["phase"] == "A"

    def test_no_op_when_already_phase_a(self, tmp_path, capsys):
        initial = {"NVDA": {"phase": "A", "shares_held": 0, "cost_basis": None}}
        state_path = _run_exit(tmp_path, "NVDA", initial_state=initial)
        state = _read_state(state_path)
        assert state["NVDA"]["phase"] == "A"

    def test_normalizes_ticker_to_uppercase(self, tmp_path):
        state_path = _run_exit(
            tmp_path, "nvda",
            initial_state={"NVDA": {"phase": "B", "shares_held": 100, "cost_basis": 880.0}}
        )
        state = _read_state(state_path)
        assert state["NVDA"]["phase"] == "A"

    def test_creates_state_file_for_unknown_ticker(self, tmp_path):
        """Exit on unknown ticker should write phase A entry without crashing."""
        state_path = _run_exit(tmp_path, "TSLA")
        state = _read_state(state_path)
        assert state["TSLA"]["phase"] == "A"
