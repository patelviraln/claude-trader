"""Unit tests for src/router.py — strategy resolution and config loading."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.router import (
    build_strategy,
    grouped_assignments,
    load_router,
    resolve_strategy,
    scheduler_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_strategies_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "strategies.toml"
    p.write_text(content, encoding="utf-8")
    return p


_SAMPLE_TOML = """\
[router]
default_strategy = "wheel"

[router.assignments]
NVDA = "wheel"
SPY  = "put_credit_spread"

[scheduler]
adapter    = "alpaca"
run_hour   = 9
run_minute = 35
timezone   = "America/New_York"

[strategies.wheel]
config_path = "config/wheel.toml"
"""


# ---------------------------------------------------------------------------
# load_router
# ---------------------------------------------------------------------------

class TestLoadRouter:
    def test_loads_router_section(self, tmp_path):
        p = _write_strategies_toml(tmp_path, _SAMPLE_TOML)
        cfg = load_router(p)
        assert cfg["router"]["default_strategy"] == "wheel"

    def test_loads_assignments(self, tmp_path):
        p = _write_strategies_toml(tmp_path, _SAMPLE_TOML)
        cfg = load_router(p)
        assert cfg["router"]["assignments"]["NVDA"] == "wheel"
        assert cfg["router"]["assignments"]["SPY"] == "put_credit_spread"

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_router(tmp_path / "nonexistent.toml")


# ---------------------------------------------------------------------------
# resolve_strategy
# ---------------------------------------------------------------------------

class TestResolveStrategy:
    def test_resolves_explicit_assignment(self, tmp_path):
        p = _write_strategies_toml(tmp_path, _SAMPLE_TOML)
        cfg = load_router(p)
        assert resolve_strategy("NVDA", cfg) == "wheel"
        assert resolve_strategy("SPY", cfg) == "put_credit_spread"

    def test_falls_back_to_default(self, tmp_path):
        p = _write_strategies_toml(tmp_path, _SAMPLE_TOML)
        cfg = load_router(p)
        assert resolve_strategy("UNKNOWN_TICKER", cfg) == "wheel"

    def test_custom_default_strategy(self, tmp_path):
        toml = "[router]\ndefault_strategy = \"rsi2\"\n[router.assignments]\n"
        p = _write_strategies_toml(tmp_path, toml)
        cfg = load_router(p)
        assert resolve_strategy("WHATEVER", cfg) == "rsi2"


# ---------------------------------------------------------------------------
# grouped_assignments
# ---------------------------------------------------------------------------

class TestGroupedAssignments:
    def test_groups_by_strategy(self, tmp_path):
        p = _write_strategies_toml(tmp_path, _SAMPLE_TOML)
        cfg = load_router(p)
        groups = grouped_assignments(cfg)
        assert "wheel" in groups
        assert "NVDA" in groups["wheel"]
        assert "put_credit_spread" in groups
        assert "SPY" in groups["put_credit_spread"]


# ---------------------------------------------------------------------------
# scheduler_config
# ---------------------------------------------------------------------------

class TestSchedulerConfig:
    def test_extracts_scheduler_section(self, tmp_path):
        p = _write_strategies_toml(tmp_path, _SAMPLE_TOML)
        cfg = load_router(p)
        sched = scheduler_config(cfg)
        assert sched["adapter"] == "alpaca"
        assert sched["run_hour"] == 9

    def test_returns_empty_when_missing(self, tmp_path):
        toml = "[router]\ndefault_strategy = \"wheel\"\n[router.assignments]\n"
        p = _write_strategies_toml(tmp_path, toml)
        cfg = load_router(p)
        assert scheduler_config(cfg) == {}


# ---------------------------------------------------------------------------
# build_strategy
# ---------------------------------------------------------------------------

class TestBuildStrategy:
    def test_builds_wheel_strategy_from_real_config(self, tmp_path):
        p = _write_strategies_toml(tmp_path, _SAMPLE_TOML)
        cfg = load_router(p)
        strategy = build_strategy("wheel", cfg)
        assert strategy.name == "wheel"

    def test_raises_for_unconfigured_non_wheel_strategy(self, tmp_path):
        toml = "[router]\ndefault_strategy = \"wheel\"\n[router.assignments]\n[strategies.wheel]\nconfig_path = \"config/wheel.toml\"\n"
        p = _write_strategies_toml(tmp_path, toml)
        cfg = load_router(p)
        with pytest.raises(KeyError, match="config_path"):
            build_strategy("put_credit_spread", cfg)

    def test_raises_for_unregistered_strategy_name(self, tmp_path):
        toml = "[router]\ndefault_strategy = \"wheel\"\n[router.assignments]\n[strategies.nonexistent]\nconfig_path = \"config/wheel.toml\"\n"
        p = _write_strategies_toml(tmp_path, toml)
        cfg = load_router(p)
        with pytest.raises(KeyError):
            build_strategy("nonexistent", cfg)
