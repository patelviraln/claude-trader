"""
Phase 9 — exit rules & position lifecycle.

Evaluates open short-option positions against standard premium-selling
exit rules and (optionally) closes them:

  profit_target — close when >= N% of the collected premium has decayed (default 50%)
  dte_close     — close anything with <= N days to expiry (default 21), win or lose
  stop_loss     — close when the option trades at >= N x premium collected (default 2.0)

Config lives in [exit_rules] of config/strategies.toml (or wheel.toml for the
legacy single-strategy setup). The scheduler runs exits before new entries.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
from src.positions import Position, append_position_event

logger = structlog.get_logger(__name__)

DEFAULT_RULES: dict[str, Any] = {
    "enabled": True,
    "profit_target_pct": 50.0,
    "dte_close": 21,
    "stop_loss_multiple": 2.0,
}


def load_exit_rules(config_path: str | Path = "config/strategies.toml") -> dict[str, Any]:
    """Read [exit_rules] from a config TOML, falling back to defaults."""
    p = Path(config_path)
    if not p.exists():
        return dict(DEFAULT_RULES)
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    return {**DEFAULT_RULES, **raw.get("exit_rules", {})}


class ExitDecision(BaseModel):
    symbol: str
    underlying: str
    strategy_name: str | None = None
    reason: str          # "profit_target" | "dte_close" | "stop_loss"
    detail: str


def evaluate_position(position: Position, rules: dict[str, Any]) -> ExitDecision | None:
    """Return an ExitDecision when a rule fires, else None.

    Only short option positions are managed — long positions and equity
    (RSI2 exits are signal-driven, not decay-driven) are left alone.
    """
    if position.asset_class != "option" or position.side != "short":
        return None

    def _decision(reason: str, detail: str) -> ExitDecision:
        return ExitDecision(
            symbol=position.symbol,
            underlying=position.underlying,
            strategy_name=position.strategy_name,
            reason=reason,
            detail=detail,
        )

    # Stop-loss first: a blown-out short trumps everything
    stop_mult = float(rules.get("stop_loss_multiple", 2.0))
    if (
        stop_mult > 0
        and position.current_price is not None
        and position.avg_entry_price > 0
        and position.current_price >= stop_mult * position.avg_entry_price
    ):
        return _decision(
            "stop_loss",
            f"mark {position.current_price:.2f} >= {stop_mult:g}x premium {position.avg_entry_price:.2f}",
        )

    profit_target = float(rules.get("profit_target_pct", 50.0))
    if (
        profit_target > 0
        and position.pct_max_profit is not None
        and position.pct_max_profit >= profit_target
    ):
        return _decision(
            "profit_target",
            f"{position.pct_max_profit:.1f}% of max profit captured (target {profit_target:g}%)",
        )

    dte_close = int(rules.get("dte_close", 21))
    if dte_close > 0 and position.dte is not None and position.dte <= dte_close:
        return _decision(
            "dte_close",
            f"{position.dte} DTE <= {dte_close} DTE close rule",
        )

    return None


def evaluate_positions(positions: list[Position], rules: dict[str, Any]) -> list[ExitDecision]:
    """Evaluate all positions; returns only the ones with a firing rule."""
    decisions = []
    for pos in positions:
        d = evaluate_position(pos, rules)
        if d is not None:
            decisions.append(d)
    return decisions


def run_exits(
    executor,
    rules: dict[str, Any] | None = None,
    router_cfg: dict | None = None,
    dry_run: bool = False,
    log_path: str | Path | None = None,
) -> list[ExitDecision]:
    """Fetch positions, evaluate exit rules, and close what fires.

    Returns the decisions (fired rules) whether or not orders were placed.
    dry_run=True evaluates without submitting close orders (dashboard preview).
    """
    from src.positions import POSITIONS_LOG_PATH, fetch_positions

    rules = rules if rules is not None else load_exit_rules()
    if not rules.get("enabled", True):
        logger.info("exit_engine.disabled")
        return []

    positions = fetch_positions(executor, router_cfg)
    decisions = evaluate_positions(positions, rules)
    logger.info("exit_engine.evaluated", positions=len(positions), exits=len(decisions))

    if dry_run:
        return decisions

    for d in decisions:
        try:
            result = executor.close_position(d.symbol)
            append_position_event(
                {
                    "event": "exit_close_submitted",
                    "symbol": d.symbol,
                    "underlying": d.underlying,
                    "strategy": d.strategy_name,
                    "reason": d.reason,
                    "detail": d.detail,
                    "order_id": result.get("order_id"),
                    "status": result.get("status"),
                },
                path=log_path or POSITIONS_LOG_PATH,
            )
            logger.info("exit_triggered", symbol=d.symbol, reason=d.reason, detail=d.detail)
        except Exception as exc:
            logger.error("exit_close_failed", symbol=d.symbol, reason=d.reason, error=str(exc))

    return decisions
