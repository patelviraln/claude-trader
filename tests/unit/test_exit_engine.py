"""Unit tests for src/exit_engine.py (Phase 9)."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.exit_engine import (
    DEFAULT_RULES,
    ExitDecision,
    evaluate_position,
    evaluate_positions,
    load_exit_rules,
    run_exits,
)
from src.positions import Position


def _short_put(pct_max_profit=None, dte=None, current_price=3.0,
               avg_entry_price=7.5, side="short", asset_class="option") -> Position:
    return Position(
        symbol="NVDA260626P00840000",
        underlying="NVDA",
        asset_class=asset_class,
        side=side,
        qty=1,
        avg_entry_price=avg_entry_price,
        current_price=current_price,
        dte=dte,
        pct_max_profit=pct_max_profit,
        strategy_name="wheel",
    )


class TestEvaluatePosition:
    def test_profit_target_fires(self):
        pos = _short_put(pct_max_profit=62.0, dte=30)
        d = evaluate_position(pos, DEFAULT_RULES)
        assert d is not None and d.reason == "profit_target"

    def test_dte_close_fires(self):
        pos = _short_put(pct_max_profit=20.0, dte=21)
        d = evaluate_position(pos, DEFAULT_RULES)
        assert d is not None and d.reason == "dte_close"

    def test_stop_loss_fires_and_beats_dte(self):
        pos = _short_put(pct_max_profit=-110.0, dte=10, current_price=16.0)
        d = evaluate_position(pos, DEFAULT_RULES)
        assert d is not None and d.reason == "stop_loss"

    def test_no_rule_fires(self):
        pos = _short_put(pct_max_profit=30.0, dte=35, current_price=5.0)
        assert evaluate_position(pos, DEFAULT_RULES) is None

    def test_long_positions_skipped(self):
        pos = _short_put(pct_max_profit=90.0, dte=5, side="long")
        assert evaluate_position(pos, DEFAULT_RULES) is None

    def test_equity_positions_skipped(self):
        pos = _short_put(pct_max_profit=None, dte=None, asset_class="equity")
        assert evaluate_position(pos, DEFAULT_RULES) is None

    def test_disabled_thresholds(self):
        rules = {"profit_target_pct": 0, "dte_close": 0, "stop_loss_multiple": 0}
        pos = _short_put(pct_max_profit=99.0, dte=1, current_price=50.0)
        assert evaluate_position(pos, rules) is None

    def test_custom_thresholds(self):
        rules = dict(DEFAULT_RULES, profit_target_pct=25.0)
        pos = _short_put(pct_max_profit=30.0, dte=40)
        d = evaluate_position(pos, rules)
        assert d is not None and d.reason == "profit_target"


class TestEvaluatePositions:
    def test_returns_only_firing(self):
        winners = _short_put(pct_max_profit=80.0, dte=30)
        holder  = _short_put(pct_max_profit=10.0, dte=40)
        assert len(evaluate_positions([winners, holder], DEFAULT_RULES)) == 1


class TestLoadExitRules:
    def test_defaults_when_file_missing(self, tmp_path):
        rules = load_exit_rules(tmp_path / "nope.toml")
        assert rules == DEFAULT_RULES

    def test_merges_file_over_defaults(self, tmp_path):
        p = tmp_path / "strategies.toml"
        p.write_text("[exit_rules]\nprofit_target_pct = 60.0\n", encoding="utf-8")
        rules = load_exit_rules(p)
        assert rules["profit_target_pct"] == 60.0
        assert rules["dte_close"] == 21  # default preserved

    def test_project_config_has_exit_rules(self):
        rules = load_exit_rules("config/strategies.toml")
        assert rules["enabled"] is True
        assert rules["profit_target_pct"] == 50.0


class TestRunExits:
    def _executor_with(self, raw_positions):
        executor = MagicMock()
        executor.get_positions.return_value = raw_positions
        executor.close_position.return_value = {"order_id": "ord-1", "status": "accepted"}
        return executor

    def _raw_winner(self):
        # sold 7.50, now 1.00 → 86.7% captured → profit_target fires
        return {
            "symbol": "NVDA260626P00840000", "asset_class": "us_option",
            "qty": "-1", "side": "PositionSide.SHORT",
            "avg_entry_price": "7.50", "current_price": "1.00",
            "market_value": "-100.0", "unrealized_pl": "650.0", "unrealized_plpc": "0.86",
        }

    def test_closes_and_logs(self, tmp_path):
        log = tmp_path / "positions.jsonl"
        executor = self._executor_with([self._raw_winner()])
        decisions = run_exits(executor, rules=DEFAULT_RULES, log_path=log)

        assert len(decisions) == 1
        assert decisions[0].reason == "profit_target"
        executor.close_position.assert_called_once_with("NVDA260626P00840000")

        from src.positions import load_position_events
        events = load_position_events(log)
        assert events[0]["event"] == "exit_close_submitted"
        assert events[0]["reason"] == "profit_target"
        assert events[0]["order_id"] == "ord-1"

    def test_dry_run_places_no_orders(self, tmp_path):
        executor = self._executor_with([self._raw_winner()])
        decisions = run_exits(executor, rules=DEFAULT_RULES, dry_run=True,
                              log_path=tmp_path / "positions.jsonl")
        assert len(decisions) == 1
        executor.close_position.assert_not_called()

    def test_disabled_rules_skip_everything(self, tmp_path):
        executor = self._executor_with([self._raw_winner()])
        decisions = run_exits(executor, rules={"enabled": False},
                              log_path=tmp_path / "positions.jsonl")
        assert decisions == []
        executor.get_positions.assert_not_called()

    def test_close_failure_does_not_raise(self, tmp_path):
        executor = self._executor_with([self._raw_winner()])
        executor.close_position.side_effect = RuntimeError("market closed")
        decisions = run_exits(executor, rules=DEFAULT_RULES,
                              log_path=tmp_path / "positions.jsonl")
        assert len(decisions) == 1  # decision still reported
