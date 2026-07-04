"""Unit tests for src/risk_manager.py (Phase 10)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.positions import Position
from src.risk_manager import (
    DEFAULT_RISK,
    RiskManager,
    capital_per_unit,
    load_risk_rules,
    validate_order,
)
from src.signal_output import Leg, SignalCard

ACCOUNT = {"equity": 100_000.0, "buying_power": 200_000.0,
           "options_buying_power": 100_000.0, "cash": 100_000.0}


def _sell_put_card(ticker="NVDA", strike=90.0, qty=1) -> SignalCard:
    return SignalCard(
        strategy_name="wheel", ticker=ticker, signal_type="SELL_PUT",
        underlying_price=strike / 0.95,
        legs=[Leg(asset_class="option", symbol=ticker, side="sell", qty=qty,
                  limit_price=2.0, strike=strike, expiry="2026-08-21",
                  option_type="put")],
    )


def _buy_equity_card(ticker="TLT", price=100.0, qty=12) -> SignalCard:
    return SignalCard(
        strategy_name="rsi2", ticker=ticker, signal_type="BUY_EQUITY",
        underlying_price=price,
        legs=[Leg(asset_class="equity", symbol=ticker, side="buy", qty=qty,
                  limit_price=price)],
    )


def _spread_card(ticker="SPY", short=500.0, long=490.0, credit=1.2) -> SignalCard:
    return SignalCard(
        strategy_name="put_credit_spread", ticker=ticker,
        signal_type="SELL_PUT_SPREAD", underlying_price=short / 0.95,
        payload={"net_credit": credit},
        legs=[
            Leg(asset_class="option", symbol=ticker, side="sell", qty=1,
                limit_price=credit, strike=short, expiry="2026-08-21", option_type="put"),
            Leg(asset_class="option", symbol=ticker, side="buy", qty=1,
                strike=long, expiry="2026-08-21", option_type="put"),
        ],
    )


def _open_position(underlying="AAPL") -> Position:
    return Position(symbol=f"{underlying}260821P00200000", underlying=underlying,
                    asset_class="option", side="short", qty=1, avg_entry_price=3.0)


class TestCapitalPerUnit:
    def test_sell_put_is_strike_collateral(self):
        assert capital_per_unit(_sell_put_card(strike=90.0)) == 9_000.0

    def test_spread_is_max_loss(self):
        # width 10 - credit 1.2 = 8.8 → $880
        assert capital_per_unit(_spread_card()) == pytest.approx(880.0)

    def test_buy_equity_is_share_price(self):
        assert capital_per_unit(_buy_equity_card(price=100.0)) == 100.0

    def test_covered_and_closing_need_nothing(self):
        card = _sell_put_card()
        card.signal_type = "SELL_CALL"
        assert capital_per_unit(card) == 0.0


class TestValidate:
    def test_approves_within_limits(self):
        # $9k collateral vs 10% of $100k equity = $10k budget
        rules = dict(DEFAULT_RISK, max_position_pct=10.0)
        d = RiskManager(rules, ACCOUNT, []).validate(_sell_put_card(strike=90.0))
        assert d.approved is True
        assert d.approved_qty == 1
        assert d.required_capital == 9_000.0

    def test_rejects_oversized_position(self):
        # default 5% of $100k = $5k budget < $9k collateral
        d = RiskManager(DEFAULT_RISK, ACCOUNT, []).validate(_sell_put_card(strike=90.0))
        assert d.approved is False
        assert "sizing" in d.reason

    def test_downsizes_equity_to_budget(self):
        # 5% of 100k = $5k budget; 100 shares @ $100 requested → 50 approved
        d = RiskManager(DEFAULT_RISK, ACCOUNT, []).validate(
            _buy_equity_card(price=100.0, qty=100))
        assert d.approved is True
        assert d.approved_qty == 50

    def test_rejects_on_per_ticker_concentration(self):
        rules = dict(DEFAULT_RISK, max_position_pct=10.0)
        d = RiskManager(rules, ACCOUNT, [_open_position("NVDA")]).validate(
            _sell_put_card("NVDA", strike=90.0))
        assert d.approved is False
        assert "per-ticker" in d.reason

    def test_rejects_on_total_concentration(self):
        rules = dict(DEFAULT_RISK, max_position_pct=10.0, max_positions_total=2)
        open_positions = [_open_position("AAPL"), _open_position("MSFT")]
        d = RiskManager(rules, ACCOUNT, open_positions).validate(_sell_put_card(strike=90.0))
        assert d.approved is False
        assert "total cap" in d.reason

    def test_rejects_on_buying_power_reserve(self):
        # BP $10k, reserve 20% of $100k equity = $20k → nothing free
        account = dict(ACCOUNT, options_buying_power=10_000.0)
        rules = dict(DEFAULT_RISK, max_position_pct=10.0)
        d = RiskManager(rules, account, []).validate(_sell_put_card(strike=90.0))
        assert d.approved is False
        assert "buying power" in d.reason

    def test_closing_trades_always_approved(self):
        card = _buy_equity_card()
        card.signal_type = "SELL_EQUITY"
        card.legs[0].side = "sell"
        d = RiskManager(DEFAULT_RISK, {"equity": 0.0},
                        [_open_position(u) for u in "ABCDEFGHIJKL"]).validate(card)
        assert d.approved is True

    def test_disabled_rules_approve_everything(self):
        d = RiskManager({"enabled": False}, {}, []).validate(_sell_put_card(strike=900.0))
        assert d.approved is True

    def test_refuses_without_equity_data(self):
        d = RiskManager(DEFAULT_RISK, {}, []).validate(_sell_put_card())
        assert d.approved is False
        assert "equity unavailable" in d.reason

    def test_spread_fits_default_budget(self):
        # $880 max loss vs $5k budget → approved
        d = RiskManager(DEFAULT_RISK, ACCOUNT, []).validate(_spread_card())
        assert d.approved is True

    def test_zero_limits_disable_individual_checks(self):
        rules = dict(DEFAULT_RISK, max_positions_per_ticker=0, max_positions_total=0,
                     max_position_pct=0, min_buying_power_pct=0)
        open_positions = [_open_position("NVDA")] * 3
        d = RiskManager(rules, ACCOUNT, open_positions).validate(
            _sell_put_card("NVDA", strike=900.0))
        assert d.approved is True


class TestValidateOrder:
    def test_fetches_account_and_positions(self):
        executor = MagicMock()
        executor.get_account.return_value = ACCOUNT
        executor.get_positions.return_value = []
        rules = dict(DEFAULT_RISK, max_position_pct=10.0)
        d = validate_order(_sell_put_card(strike=90.0), executor, rules)
        assert d.approved is True

    def test_rejects_when_account_unavailable(self):
        executor = MagicMock()
        executor.get_account.side_effect = RuntimeError("api down")
        d = validate_order(_sell_put_card(), executor, DEFAULT_RISK)
        assert d.approved is False
        assert "account state unavailable" in d.reason


class TestLoadRiskRules:
    def test_defaults_when_missing(self, tmp_path):
        assert load_risk_rules(tmp_path / "nope.toml") == DEFAULT_RISK

    def test_project_config_has_risk_section(self):
        rules = load_risk_rules("config/strategies.toml")
        assert rules["enabled"] is True
        assert rules["max_position_pct"] == 5.0


class TestExecuteCardGating:
    """signal_engine._execute_card applies decisions to the card."""

    def _executor(self, account=ACCOUNT, positions=None):
        ex = MagicMock()
        ex.get_account.return_value = account
        ex.get_positions.return_value = positions or []
        result = MagicMock()
        result.order_id, result.status = "ord-1", "accepted"
        ex.execute.return_value = [result]
        return ex

    def test_blocked_order_annotates_card_no_order(self):
        from src.signal_engine import _execute_card
        card = _sell_put_card(strike=90.0)  # exceeds default 5% budget
        ex = self._executor()
        _execute_card(card, ex)
        assert card.order_ids == []
        assert any("risk_blocked" in f for f in card.risk_flags)
        ex.execute.assert_not_called()

    def test_approved_order_placed_with_intent(self):
        from src.signal_engine import _execute_card
        card = _spread_card()  # $880 max loss — within budget
        ex = self._executor()
        _execute_card(card, ex)
        assert card.order_ids == ["ord-1"]
        assert card.max_contracts == 1
        ex.execute.assert_called_once()

    def test_downsized_equity_qty_applied(self):
        from src.signal_engine import _execute_card
        card = _buy_equity_card(price=100.0, qty=100)
        ex = self._executor()
        _execute_card(card, ex)
        assert card.legs[0].qty == 50
        assert any("risk_downsized" in f for f in card.risk_flags)