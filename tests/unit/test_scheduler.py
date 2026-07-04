"""Unit tests for scheduler.py — run_job logic, config loading, adapter building."""
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_toml(tmp_path: Path, scheduler_section: dict) -> Path:
    """Write a minimal wheel.toml with a [scheduler] section and return its path."""
    lines = ["[wheel]\nema_period = 50\n\n[scheduler]\n"]
    for k, v in scheduler_section.items():
        if isinstance(v, list):
            items = ", ".join(f'"{x}"' for x in v)
            lines.append(f'{k} = [{items}]\n')
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"\n')
        else:
            lines.append(f'{k} = {v}\n')
    config_path = tmp_path / "wheel.toml"
    config_path.write_text("".join(lines), encoding="utf-8")
    return config_path


def _write_router_toml(tmp_path: Path, assignments: dict[str, str]) -> Path:
    """Write a minimal strategies.toml-style router config and return its path."""
    lines = ['[router]\ndefault_strategy = "wheel"\n\n[router.assignments]\n']
    for ticker, strat in assignments.items():
        lines.append(f'{ticker} = "{strat}"\n')
    lines.append('\n[scheduler]\nadapter = "fixture"\n')
    config_path = tmp_path / "strategies.toml"
    config_path.write_text("".join(lines), encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# load_scheduler_config
# ---------------------------------------------------------------------------

class TestLoadSchedulerConfig:
    def test_reads_tickers(self, tmp_path):
        cfg = _write_toml(tmp_path, {"tickers": ["AAPL", "NVDA"], "adapter": "fixture"})
        from scheduler import load_scheduler_config
        result = load_scheduler_config(cfg)
        assert result["tickers"] == ["AAPL", "NVDA"]

    def test_reads_run_time(self, tmp_path):
        cfg = _write_toml(tmp_path, {"tickers": ["AAPL"], "run_hour": 10, "run_minute": 0})
        from scheduler import load_scheduler_config
        result = load_scheduler_config(cfg)
        assert result["run_hour"] == 10
        assert result["run_minute"] == 0

    def test_returns_empty_dict_when_section_missing(self, tmp_path):
        config_path = tmp_path / "wheel.toml"
        config_path.write_text("[wheel]\nema_period = 50\n", encoding="utf-8")
        from scheduler import load_scheduler_config
        result = load_scheduler_config(config_path)
        assert result == {}


# ---------------------------------------------------------------------------
# build_adapter
# ---------------------------------------------------------------------------

class TestBuildAdapter:
    def test_builds_fixture_adapter(self):
        from scheduler import build_adapter
        adapter = build_adapter("fixture")
        from src.adapters.fixture_adapter import FixtureAdapter
        assert isinstance(adapter, FixtureAdapter)

    def test_raises_on_unknown_adapter(self):
        from scheduler import build_adapter
        with pytest.raises(ValueError, match="Unknown adapter"):
            build_adapter("tradier")


# ---------------------------------------------------------------------------
# run_job
# ---------------------------------------------------------------------------

class TestRunJob:
    def test_calls_run_signals_batch_for_each_ticker(self, tmp_path):
        cfg = _write_toml(tmp_path, {"tickers": ["AAPL", "MSFT"], "adapter": "fixture"})

        mock_card = MagicMock()
        mock_card.signal_type = "SELL_PUT"

        with patch("scheduler.build_adapter") as mock_build, \
             patch("scheduler.run_signals_batch", return_value=[mock_card]) as mock_run:
            from scheduler import run_job
            run_job(cfg)

        assert mock_run.call_count == 2
        called_tickers = [c.kwargs["tickers"][0] for c in mock_run.call_args_list]
        assert called_tickers == ["AAPL", "MSFT"]

    def test_skips_run_when_no_tickers_configured(self, tmp_path):
        cfg = _write_toml(tmp_path, {"tickers": [], "adapter": "fixture"})
        with patch("scheduler.run_signals_batch") as mock_run:
            from scheduler import run_job
            run_job(cfg)
        mock_run.assert_not_called()

    def test_continues_after_per_ticker_error(self, tmp_path):
        cfg = _write_toml(tmp_path, {"tickers": ["AAPL", "MSFT", "NVDA"], "adapter": "fixture"})

        mock_card = MagicMock()
        mock_card.signal_type = "SELL_PUT"

        def side_effect(*args, **kwargs):
            if kwargs.get("tickers") == ["MSFT"]:
                raise RuntimeError("API timeout")
            return [mock_card]

        with patch("scheduler.build_adapter"), \
             patch("scheduler.run_signals_batch", side_effect=side_effect) as mock_run:
            from scheduler import run_job
            run_job(cfg)  # must not raise

        assert mock_run.call_count == 3

    def test_returns_early_when_adapter_fails(self, tmp_path):
        cfg = _write_toml(tmp_path, {"tickers": ["AAPL"], "adapter": "alpaca"})
        with patch("scheduler.build_adapter", side_effect=Exception("bad credentials")), \
             patch("scheduler.run_signals_batch") as mock_run:
            from scheduler import run_job
            run_job(cfg)  # must not raise
        mock_run.assert_not_called()

    def test_passes_config_path_to_run_signals_batch(self, tmp_path):
        cfg = _write_toml(tmp_path, {"tickers": ["NVDA"], "adapter": "fixture"})
        mock_card = MagicMock()
        mock_card.signal_type = "NO_SIGNAL"
        with patch("scheduler.build_adapter"), \
             patch("scheduler.run_signals_batch", return_value=[mock_card]) as mock_run:
            from scheduler import run_job
            run_job(cfg)
        assert mock_run.call_args.kwargs["config_path"] == cfg


# ---------------------------------------------------------------------------
# run_job — router mode (strategies.toml with [router] section)
# ---------------------------------------------------------------------------

class TestRunJobRouted:
    @pytest.fixture(autouse=True)
    def _no_real_notifications(self):
        """Keep test runs from appending summaries to the real logs/daily_summary.md."""
        with patch("src.notifier.send_summary"):
            yield
    def test_dispatches_each_ticker_with_its_strategy(self, tmp_path):
        cfg = _write_router_toml(tmp_path, {"AAPL": "wheel", "NVDA": "wheel", "TLT": "rsi2"})
        mock_card = MagicMock()
        mock_card.signal_type = "NO_SIGNAL"
        with patch("scheduler.build_adapter"), \
             patch("scheduler.run_signals_batch", return_value=[mock_card]) as mock_run:
            from scheduler import run_job
            run_job(cfg)

        assert mock_run.call_count == 3
        dispatched = {
            c.kwargs["tickers"][0]: c.kwargs["strategy_name"]
            for c in mock_run.call_args_list
        }
        assert dispatched == {"AAPL": "wheel", "NVDA": "wheel", "TLT": "rsi2"}
        assert all(c.kwargs["router_config_path"] == cfg for c in mock_run.call_args_list)

    def test_continues_after_per_ticker_error(self, tmp_path):
        cfg = _write_router_toml(tmp_path, {"AAPL": "wheel", "TLT": "rsi2"})
        mock_card = MagicMock()
        mock_card.signal_type = "NO_SIGNAL"

        def side_effect(**kwargs):
            if kwargs["tickers"][0] == "AAPL":
                raise RuntimeError("boom")
            return [mock_card]

        with patch("scheduler.build_adapter"), \
             patch("scheduler.run_signals_batch", side_effect=side_effect) as mock_run:
            from scheduler import run_job
            run_job(cfg)  # must not raise

        assert mock_run.call_count == 2

    def test_skips_when_no_assignments(self, tmp_path):
        cfg = _write_router_toml(tmp_path, {})
        with patch("scheduler.run_signals_batch") as mock_run:
            from scheduler import run_job
            run_job(cfg)
        mock_run.assert_not_called()
