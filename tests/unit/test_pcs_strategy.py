"""
Unit tests for PutCreditSpreadStrategy — state schema, filter chain, registry,
simulate_trade, and router integration.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.strategies.base import FilterResult
from src.strategies.spreads.state import PCSState
from src.strategies.spreads.strategy import PutCreditSpreadStrategy


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_PCS_CONFIG = {
    "ema_period": 10,        # short EMA for synthetic data
    "rsi_min": 20.0,
    "rsi_max": 100.0,        # allow RSI=100 from synthetic linear price data
    "dte_min": 30,
    "dte_max": 45,
    "short_delta_target": 0.30,
    "short_delta_tolerance": 0.05,
    "long_delta_target": 0.15,
    "long_delta_tolerance": 0.07,
    "min_iv_rank": 5.0,       # low so fixture IV history passes
    "min_credit": 0.10,       # low so fixture mid prices pass
    "max_spread_width": 20.0,
}


def _rising_df(n: int = 60, start: float = 100.0) -> pd.DataFrame:
    prices = [start + i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices],
        "low":  [p - 1 for p in prices], "close": prices,
        "volume": [1_000_000] * n,
    })


def _falling_df(n: int = 60, start: float = 130.0) -> pd.DataFrame:
    prices = [start - i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices],
        "low":  [max(0.01, p - 1) for p in prices], "close": prices,
        "volume": [1_000_000] * n,
    })


def _make_chain(dte: int = 37) -> pd.DataFrame:
    expiry = (datetime.date(2026, 5, 16) + datetime.timedelta(days=dte)).isoformat()
    rows = []
    # Varying bid/ask so net_credit is positive (short_mid > long_mid)
    put_data = [
        (75,  -0.05, 0.05, 0.10),
        (80,  -0.10, 0.15, 0.25),
        (85,  -0.15, 0.40, 0.60),   # long leg candidate
        (90,  -0.20, 0.70, 0.90),
        (95,  -0.30, 1.20, 1.40),   # short leg candidate
        (100, -0.50, 2.50, 2.80),
    ]
    for strike, put_delta, bid, ask in put_data:
        rows.append({
            "strike": float(strike), "expiry": expiry, "option_type": "put",
            "delta": put_delta, "iv": 0.35, "bid": bid, "ask": ask,
            "volume": 500, "dte": dte,
        })
    for strike, call_delta in [(100, 0.50), (105, 0.30), (110, 0.15)]:
        rows.append({
            "strike": float(strike), "expiry": expiry, "option_type": "call",
            "delta": call_delta, "iv": 0.30, "bid": 0.8, "ask": 1.0,
            "volume": 300, "dte": dte,
        })
    return pd.DataFrame(rows)


class _MockAdapter:
    def __init__(self, ohlcv: pd.DataFrame, chain: pd.DataFrame):
        self._ohlcv = ohlcv
        self._chain = chain

    def get_ohlcv(self, ticker: str, bars: int = 60) -> pd.DataFrame:
        return self._ohlcv.tail(bars).copy()

    def get_options_chain(self, ticker: str, dte_min: int, dte_max: int) -> pd.DataFrame:
        return self._chain.copy()

    def get_historical_ohlcv(self, ticker, start_date, end_date):
        return self._ohlcv


# ---------------------------------------------------------------------------
# PCSState
# ---------------------------------------------------------------------------

class TestPCSState:
    def test_default_has_no_open_spread(self):
        s = PCSState.default()
        assert s.open_spread is False
        assert s.short_strike is None
        assert s.long_strike is None

    def test_from_dict_round_trips(self):
        data = {
            "open_spread": True,
            "short_strike": 95.0,
            "long_strike": 90.0,
            "expiry": "2026-06-22",
            "entry_credit": 0.75,
            "entry_date": "2026-05-16",
        }
        s = PCSState.from_dict(data)
        assert s.open_spread is True
        assert s.short_strike == 95.0
        assert s.long_strike == 90.0

    def test_from_dict_ignores_unknown_keys(self):
        s = PCSState.from_dict({"open_spread": False, "garbage_key": 123})
        assert s.open_spread is False

    def test_from_empty_dict_gives_defaults(self):
        s = PCSState.from_dict({})
        assert s == PCSState.default()


# ---------------------------------------------------------------------------
# PutCreditSpreadStrategy — protocol compliance
# ---------------------------------------------------------------------------

class TestPCSStrategyProtocol:
    def test_name_attribute(self):
        s = PutCreditSpreadStrategy()
        assert s.name == "put_credit_spread"

    def test_get_state_schema_returns_pcs_state(self):
        s = PutCreditSpreadStrategy()
        assert s.get_state_schema() is PCSState

    def test_load_config_sets_up_filters(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        assert len(s._filters) == 6

    def test_evaluate_raises_without_load_config(self):
        s = PutCreditSpreadStrategy()
        with pytest.raises(RuntimeError, match="load_config"):
            s.evaluate("SPY", _rising_df(), _make_chain(), {})

    def test_emit_signal_card_raises_without_load_config(self):
        s = PutCreditSpreadStrategy()
        adapter = _MockAdapter(_rising_df(), _make_chain())
        with pytest.raises(RuntimeError, match="load_config"):
            s.emit_signal_card("SPY", adapter, {})


# ---------------------------------------------------------------------------
# evaluate() — filter chain execution
# ---------------------------------------------------------------------------

class TestPCSEvaluate:
    def test_open_spread_state_hard_stops_immediately(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        open_state = {"open_spread": True, "short_strike": 95.0, "long_strike": 90.0,
                      "expiry": "2026-06-22", "entry_credit": 0.80, "entry_date": "2026-05-16"}
        results = s.evaluate("SPY", _rising_df(), _make_chain(), open_state)
        assert len(results) == 1  # only SpreadOpenGate ran
        assert not results[0].passed
        assert results[0].hard_stop

    def test_falling_trend_hard_stops_at_ema(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        results = s.evaluate("SPY", _falling_df(), _make_chain(), {})
        ema_result = next(r for r in results if r.filter_name == "ema_trend")
        assert not ema_result.passed
        assert ema_result.hard_stop

    def test_rising_trend_with_valid_chain_all_pass(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        results = s.evaluate("SPY", _rising_df(), _make_chain(), {}, iv_rank=50.0)
        hard_stops = [r for r in results if not r.passed and r.hard_stop]
        assert hard_stops == [], f"Unexpected hard stop(s): {[r.filter_name for r in hard_stops]}"
        assert all(r.passed for r in results)

    def test_all_six_filters_run_on_happy_path(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        results = s.evaluate("SPY", _rising_df(), _make_chain(), {}, iv_rank=50.0)
        assert len(results) == 6

    def test_filter_names_in_correct_order(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        results = s.evaluate("SPY", _rising_df(), _make_chain(), {}, iv_rank=50.0)
        names = [r.filter_name for r in results]
        assert names[0] == "spread_open_gate"
        assert names[-1] == "spread_leg_builder"

    def test_low_iv_rank_hard_stops(self):
        s = PutCreditSpreadStrategy()
        cfg = {**_PCS_CONFIG, "min_iv_rank": 50.0}
        s.load_config(cfg)
        results = s.evaluate("SPY", _rising_df(), _make_chain(), {}, iv_rank=10.0)
        iv_result = next(r for r in results if r.filter_name == "iv_rank_gate")
        assert not iv_result.passed
        assert iv_result.hard_stop

    def test_spread_leg_builder_adjustments_propagate(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        results = s.evaluate("SPY", _rising_df(), _make_chain(), {}, iv_rank=40.0)
        leg_result = next(r for r in results if r.filter_name == "spread_leg_builder")
        assert leg_result.passed
        assert "short_strike" in leg_result.adjustments
        assert "long_strike" in leg_result.adjustments
        assert "net_credit" in leg_result.adjustments


# ---------------------------------------------------------------------------
# emit_signal_card() — full pipeline
# ---------------------------------------------------------------------------

class TestPCSEmitSignalCard:
    def test_happy_path_emits_sell_put_spread(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        adapter = _MockAdapter(_rising_df(), _make_chain())
        card = s.emit_signal_card("SPY", adapter, {}, context={"iv_history_dir": tmp_path})
        assert card.signal_type == "SELL_PUT_SPREAD"
        assert card.ticker == "SPY"

    def test_happy_path_has_two_legs(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        adapter = _MockAdapter(_rising_df(), _make_chain())
        card = s.emit_signal_card("SPY", adapter, {}, context={"iv_history_dir": tmp_path})
        assert len(card.legs) == 2

    def test_first_leg_is_sell(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        card = s.emit_signal_card("SPY", _MockAdapter(_rising_df(), _make_chain()), {},
                                  context={"iv_history_dir": tmp_path})
        assert card.legs[0].side == "sell"

    def test_second_leg_is_buy(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        card = s.emit_signal_card("SPY", _MockAdapter(_rising_df(), _make_chain()), {},
                                  context={"iv_history_dir": tmp_path})
        assert card.legs[1].side == "buy"

    def test_short_strike_above_long_strike(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        card = s.emit_signal_card("SPY", _MockAdapter(_rising_df(), _make_chain()), {},
                                  context={"iv_history_dir": tmp_path})
        assert card.legs[0].strike > card.legs[1].strike

    def test_payload_contains_spread_fields(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        card = s.emit_signal_card("SPY", _MockAdapter(_rising_df(), _make_chain()), {},
                                  context={"iv_history_dir": tmp_path})
        for key in ("net_credit", "spread_width", "max_profit", "max_loss", "dte"):
            assert key in card.payload, f"Missing payload key: {key}"

    def test_max_profit_equals_net_credit_times_100(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        card = s.emit_signal_card("SPY", _MockAdapter(_rising_df(), _make_chain()), {},
                                  context={"iv_history_dir": tmp_path})
        assert abs(card.payload["max_profit"] - card.payload["net_credit"] * 100) < 0.02

    def test_falling_trend_emits_no_signal(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        adapter = _MockAdapter(_falling_df(), _make_chain())
        card = s.emit_signal_card("SPY", adapter, {}, context={"iv_history_dir": tmp_path})
        assert card.signal_type == "NO_SIGNAL"
        assert card.no_signal_reason is not None

    def test_open_spread_state_emits_no_signal(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        adapter = _MockAdapter(_rising_df(), _make_chain())
        open_state = {"open_spread": True, "short_strike": 95.0, "long_strike": 90.0,
                      "expiry": "2026-06-22", "entry_credit": 0.80, "entry_date": "2026-05-16"}
        card = s.emit_signal_card("SPY", adapter, open_state, context={"iv_history_dir": tmp_path})
        assert card.signal_type == "NO_SIGNAL"

    def test_strategy_name_in_card(self, tmp_path):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        card = s.emit_signal_card("SPY", _MockAdapter(_rising_df(), _make_chain()), {},
                                  context={"iv_history_dir": tmp_path})
        assert card.strategy_name == "put_credit_spread"


# ---------------------------------------------------------------------------
# simulate_trade()
# ---------------------------------------------------------------------------

def _make_future_ohlcv(start_price: float, n: int = 60) -> pd.DataFrame:
    dates = pd.bdate_range(start="2026-05-19", periods=n, freq="B", tz="UTC")
    prices = [start_price] * n
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices],
        "low":  [p - 1 for p in prices], "close": prices,
        "volume": [1_000_000] * n,
    }, index=dates)


class TestPCSSimulateTrade:
    def _make_card(self, short: float, long: float, credit: float):
        from src.signal_output import Leg, SignalCard
        return SignalCard(
            strategy_name="put_credit_spread",
            ticker="SPY",
            signal_type="SELL_PUT_SPREAD",
            underlying_price=100.0,
            legs=[
                Leg(asset_class="option", symbol="SPY", side="sell", qty=1,
                    strike=short, expiry="2026-07-18", option_type="put",
                    delta_estimate=-0.30),
                Leg(asset_class="option", symbol="SPY", side="buy", qty=1,
                    strike=long, expiry="2026-07-18", option_type="put",
                    delta_estimate=-0.15),
            ],
            payload={"dte": 37, "net_credit": credit, "spread_width": short - long,
                     "max_profit": credit * 100, "max_loss": (short - long - credit) * 100},
        )

    def test_expired_worthless_keeps_full_credit(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        entry_ohlcv = _rising_df(60)
        # Price stays at 100, both puts (short=95, long=85) expire worthless
        future = _make_future_ohlcv(start_price=100.0)
        card = self._make_card(short=95.0, long=85.0, credit=0.80)
        result = s.simulate_trade(card, future)
        assert result.pnl == pytest.approx(0.80 * 100, abs=0.01)
        assert result.metadata["outcome"] == "EXPIRED_WORTHLESS"

    def test_max_loss_when_price_below_long_strike(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        # Price crashes to 50, far below long_strike=85 → max loss
        future = _make_future_ohlcv(start_price=50.0)
        card = self._make_card(short=95.0, long=85.0, credit=0.80)
        result = s.simulate_trade(card, future)
        expected_pnl = (0.80 - 10.0) * 100  # credit - spread_width = -9.20 per share × 100
        assert result.pnl == pytest.approx(expected_pnl, abs=0.01)
        assert result.metadata["outcome"] == "MAX_LOSS"

    def test_partial_loss_between_strikes(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        # Price lands at 91, between long_strike=85 and short_strike=95
        future = _make_future_ohlcv(start_price=91.0)
        card = self._make_card(short=95.0, long=85.0, credit=0.80)
        result = s.simulate_trade(card, future)
        # pnl = (credit - (short_strike - price)) × 100 = (0.80 - 4.0) × 100 = -320
        expected_pnl = (0.80 - (95.0 - 91.0)) * 100
        assert result.pnl == pytest.approx(expected_pnl, abs=0.01)
        assert result.metadata["outcome"] == "PARTIAL_LOSS"

    def test_returns_trade_result_with_correct_fields(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        future = _make_future_ohlcv(start_price=100.0)
        card = self._make_card(short=95.0, long=85.0, credit=0.80)
        result = s.simulate_trade(card, future)
        assert result.ticker == "SPY"
        assert result.signal_type == "SELL_PUT_SPREAD"
        assert result.entry_date is not None
        assert result.exit_date is not None
        assert "outcome" in result.metadata

    def test_no_future_data_returns_open_position(self):
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        future = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        future.index = pd.DatetimeIndex([], tz="UTC")
        card = self._make_card(short=95.0, long=85.0, credit=0.80)
        result = s.simulate_trade(card, future)
        assert result.metadata["outcome"] == "OPEN"
        assert result.pnl == 0.0

    def test_simulate_trade_with_missing_legs_returns_zero(self):
        from src.signal_output import SignalCard
        s = PutCreditSpreadStrategy()
        s.load_config(_PCS_CONFIG)
        card = SignalCard(
            strategy_name="put_credit_spread", ticker="SPY",
            signal_type="SELL_PUT_SPREAD", underlying_price=100.0, legs=[],
            payload={"dte": 37, "net_credit": 0.80},
        )
        result = s.simulate_trade(card, _make_future_ohlcv(100.0))
        assert result.pnl == 0.0


# ---------------------------------------------------------------------------
# Registry registration
# ---------------------------------------------------------------------------

class TestPCSRegistration:
    def test_strategy_registered_by_name(self):
        # Importing the strategies package triggers @register decorators
        import src.strategies  # noqa: F401
        from src.strategies.registry import get
        cls = get("put_credit_spread")
        assert cls is PutCreditSpreadStrategy

    def test_router_can_build_pcs_strategy(self, tmp_path):
        import tomllib
        from src.router import build_strategy, load_router

        toml = (
            "[router]\ndefault_strategy = \"wheel\"\n[router.assignments]\n"
            "[strategies.put_credit_spread]\nconfig_path = \"config/put_credit_spread.toml\"\n"
        )
        p = tmp_path / "strategies.toml"
        p.write_text(toml, encoding="utf-8")
        cfg = load_router(p)
        strategy = build_strategy("put_credit_spread", cfg)
        assert strategy.name == "put_credit_spread"
