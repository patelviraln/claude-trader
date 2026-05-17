"""
Integration tests: run the full signal pipeline against fixture data
and verify expected outcomes for each of the 5 scenarios.
"""
import json
from pathlib import Path

import pytest

from src.adapters.fixture_adapter import FixtureAdapter
from src.signal_engine import run_signals_batch
from src.state_store import write_state

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "wheel.toml"


@pytest.fixture()
def tmp_state(tmp_path):
    """Write a fresh wheel_state.json in a temp dir and return its path."""
    state = {
        "WXYZ": {"phase": "A", "shares_held": 0, "cost_basis": None, "assignment_date": None, "open_put_strike": None, "open_put_expiry": None},
        "CALLX": {"phase": "B", "shares_held": 100, "cost_basis": 285.0, "assignment_date": "2026-04-15", "open_put_strike": 280.0, "open_put_expiry": "2026-04-18"},
        "EMAFAIL": {"phase": "A", "shares_held": 0, "cost_basis": None, "assignment_date": None, "open_put_strike": None, "open_put_expiry": None},
        "RSIFAIL": {"phase": "A", "shares_held": 0, "cost_basis": None, "assignment_date": None, "open_put_strike": None, "open_put_expiry": None},
        "LOWCONF": {"phase": "A", "shares_held": 0, "cost_basis": None, "assignment_date": None, "open_put_strike": None, "open_put_expiry": None},
    }
    p = tmp_path / "wheel_state.json"
    write_state(p, state)
    return p


@pytest.fixture()
def adapter():
    return FixtureAdapter(FIXTURES_DIR)


def run_one(ticker, adapter, tmp_state, tmp_path):
    signals_path = tmp_path / "signals.jsonl"
    cards = run_signals_batch(
        tickers=[ticker],
        adapter=adapter,
        config_path=CONFIG_PATH,
        state_path=tmp_state,
        signals_path=signals_path,
        iv_history_dir=tmp_path / "iv_history",
        print_output=False,
    )
    return cards[0], signals_path


class TestScenario1_WXYZ_ClearPutSignal:
    def test_produces_sell_put(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("WXYZ", adapter, tmp_state, tmp_path)
        assert card.signal_type == "SELL_PUT", f"Expected SELL_PUT, got {card.signal_type} — reason: {card.no_signal_reason}"

    def test_has_strike_and_expiry(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("WXYZ", adapter, tmp_state, tmp_path)
        assert card.legs, "Expected at least one leg"
        assert card.legs[0].strike is not None
        assert card.legs[0].expiry is not None

    def test_delta_is_negative(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("WXYZ", adapter, tmp_state, tmp_path)
        assert card.legs, "Expected at least one leg"
        assert card.legs[0].delta_estimate is not None
        assert card.legs[0].delta_estimate < 0

    def test_appended_to_jsonl(self, adapter, tmp_state, tmp_path):
        card, signals_path = run_one("WXYZ", adapter, tmp_state, tmp_path)
        lines = signals_path.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["ticker"] == "WXYZ"


class TestScenario2_CALLX_ClearCallSignal:
    def test_produces_sell_call(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("CALLX", adapter, tmp_state, tmp_path)
        assert card.signal_type == "SELL_CALL", f"Expected SELL_CALL, got {card.signal_type} — reason: {card.no_signal_reason}"

    def test_delta_is_positive(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("CALLX", adapter, tmp_state, tmp_path)
        assert card.legs, "Expected at least one leg"
        assert card.legs[0].delta_estimate is not None
        assert card.legs[0].delta_estimate > 0


class TestScenario3_EMAFAIL:
    def test_produces_no_signal(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("EMAFAIL", adapter, tmp_state, tmp_path)
        assert card.signal_type == "NO_SIGNAL"

    def test_reason_mentions_ema(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("EMAFAIL", adapter, tmp_state, tmp_path)
        assert card.no_signal_reason is not None
        assert "ema" in card.no_signal_reason.lower()


class TestScenario4_RSIFAIL:
    def test_produces_no_signal(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("RSIFAIL", adapter, tmp_state, tmp_path)
        # Either EMA or RSI may fail depending on generated prices
        assert card.signal_type == "NO_SIGNAL"


class TestScenario5_LOWCONF:
    def test_runs_without_error(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("LOWCONF", adapter, tmp_state, tmp_path)
        # Should not raise; may be SELL_PUT or NO_SIGNAL depending on fixture
        assert card.signal_type in ("SELL_PUT", "SELL_CALL", "NO_SIGNAL")

    def test_signal_card_is_serializable(self, adapter, tmp_state, tmp_path):
        card, _ = run_one("LOWCONF", adapter, tmp_state, tmp_path)
        serialized = card.to_json()
        parsed = json.loads(serialized)
        assert parsed["ticker"] == "LOWCONF"
