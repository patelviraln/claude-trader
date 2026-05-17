"""
Strategy router — resolves ticker → strategy class based on config/strategies.toml.

The router is the A5 replacement for the hard-coded "always use WheelStrategy"
logic in signal_engine.run_signals_batch and scheduler.run_job.

Usage (once Track B strategies are registered):

    from src.router import load_router, resolve_strategy, build_strategy

    router_cfg = load_router()
    strategy_name = resolve_strategy("SPY", router_cfg)   # -> "put_credit_spread"
    strategy = build_strategy(strategy_name, router_cfg)  # fully configured instance
    card = strategy.emit_signal_card(ticker, adapter, state)
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from src.strategies.registry import get as get_strategy_cls

_DEFAULT_STRATEGIES_TOML = Path("config/strategies.toml")


def load_router(config_path: str | Path = _DEFAULT_STRATEGIES_TOML) -> dict[str, Any]:
    """Load the strategies.toml router config."""
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def resolve_strategy(ticker: str, router_cfg: dict[str, Any]) -> str:
    """Return the strategy name for a ticker, applying the default fallback."""
    assignments: dict[str, str] = router_cfg.get("router", {}).get("assignments", {})
    default: str = router_cfg.get("router", {}).get("default_strategy", "wheel")
    return assignments.get(ticker, default)


def build_strategy(strategy_name: str, router_cfg: dict[str, Any]):
    """Instantiate and load_config for the named strategy.

    Reads the strategy's config_path from router_cfg["strategies"][name]["config_path"].
    Falls back to config/wheel.toml for "wheel" if no explicit pointer is set.
    """
    strat_section = router_cfg.get("strategies", {}).get(strategy_name, {})
    cfg_path_str = strat_section.get("config_path")

    if cfg_path_str is None:
        if strategy_name == "wheel":
            cfg_path_str = "config/wheel.toml"
        else:
            raise KeyError(
                f"No config_path configured for strategy '{strategy_name}' in strategies.toml."
            )

    cfg_path = Path(cfg_path_str)
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    cfg = raw.get(strategy_name, raw)

    cls = get_strategy_cls(strategy_name)
    instance = cls()
    instance.load_config(cfg)
    return instance


def grouped_assignments(router_cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Return {strategy_name: [ticker, ...]} for batch dispatch in the scheduler."""
    assignments: dict[str, str] = router_cfg.get("router", {}).get("assignments", {})
    default: str = router_cfg.get("router", {}).get("default_strategy", "wheel")
    groups: dict[str, list[str]] = {}
    for ticker, strategy_name in assignments.items():
        groups.setdefault(strategy_name, []).append(ticker)
    if not groups and not assignments:
        groups[default] = []
    return groups


def scheduler_config(router_cfg: dict[str, Any]) -> dict[str, Any]:
    """Extract the [scheduler] section from a strategies.toml config."""
    return router_cfg.get("scheduler", {})
