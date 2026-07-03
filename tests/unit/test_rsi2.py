"""Unit tests for RSI2Strategy (Phase B2)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.momentum.rsi2 import RSI2Strategy
from src.strategies.momentum.state import RSI2State

CFG = {
    "rsi_period": 2,
    "entry_rsi_max": 10.0,
    "exit_rsi_min": 70.0,
    "trend_filter_ma": 200,
    "max_hold_days": 5,
    "position_size_pct": 5.0,
    "paper_starting_equity": 25000.0,
}


def make_ohlcv(
    n: int = 300,
    start_price: float = 100.0,
    drift: float = 0.0009,
    tail_rets: list[float] | None = None,
    seed: int = 7,
) -> pd.DataFrame:
    """Synthetic daily OHLCV; tail_rets overrides the last len(tail_rets) returns."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.005, n)
    if tail_rets:
        rets[-len(tail_rets):] = tail_rets
    closes = start_price * np.exp(np.cumsum(rets))
    idx = pd.bdate_range(end="2026-05-15", periods=n, tz="UTC")
    return pd.DataFrame({
        "open": closes, "high": closes * 1.004, "low": closes * 0.996,
        "close": closes, "volume": np.full(n, 1e6),
    }, index=idx)


class StubAdapter:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_ohlcv(self, ticker: str, bars: int, timeframe: str = "1Day") -> pd.DataFrame:
        return self._df.iloc[-bars:]


def make_strategy() -> RSI2Strategy:
    s = RSI2Strategy()
    s.load_config(dict(CFG))
    return s


# ---------------------------------------------------------------------------
# Entry logic
# ---------------------------------------------------------------------------

class TestEntry:
    def test_buy_equity_when_oversold_above_sma(self):
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])
        card = make_strategy().emit_signal_card("TLT", StubAdapter(df), {})
        assert card.signal_type == "BUY_EQUITY"
        assert card.strategy_name == "rsi2"
        leg = card.legs[0]
        assert leg.asset_class == "equity"
        assert leg.side == "buy"
        assert leg.strike is None and leg.expiry is None
        assert card.indicators["rsi2"] < 10

    def test_share_sizing_5pct_of_equity(self):
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])
        card = make_strategy().emit_signal_card("TLT", StubAdapter(df), {})
        close = card.underlying_price
        expected = int(0.05 * 25000.0 / close)
        assert card.legs[0].qty == expected

    def test_no_signal_below_200dma(self):
        df = make_ohlcv(drift=-0.002, tail_rets=[-0.012, -0.015, -0.011])
        card = make_strategy().emit_signal_card("TLT", StubAdapter(df), {})
        assert card.signal_type == "NO_SIGNAL"
        assert "below_200dma" in card.no_signal_reason

    def test_no_signal_rsi_not_oversold(self):
        df = make_ohlcv(tail_rets=[0.01, 0.01, 0.01])
        card = make_strategy().emit_signal_card("TLT", StubAdapter(df), {})
        assert card.signal_type == "NO_SIGNAL"
        assert "rsi_not_oversold" in card.no_signal_reason

    def test_no_signal_insufficient_history(self):
        df = make_ohlcv(n=100, tail_rets=[-0.012, -0.015, -0.011])
        card = make_strategy().emit_signal_card("TLT", StubAdapter(df), {})
        assert card.signal_type == "NO_SIGNAL"
        assert "insufficient_history" in card.no_signal_reason

    def test_no_signal_when_position_size_zero(self):
        s = RSI2Strategy()
        cfg = dict(CFG, paper_starting_equity=100.0)  # 5% = $5 < 1 share
        s.load_config(cfg)
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])
        card = s.emit_signal_card("TLT", StubAdapter(df), {})
        assert card.signal_type == "NO_SIGNAL"
        assert "position_size_zero" in card.no_signal_reason


# ---------------------------------------------------------------------------
# Exit / hold logic
# ---------------------------------------------------------------------------

class TestExit:
    def _position(self, entry_date: str = "2026-07-01") -> dict:
        return {"in_position": True, "entry_date": entry_date,
                "entry_price": 100.0, "shares": 12}

    def test_sell_on_rsi_recovery(self):
        df = make_ohlcv(tail_rets=[0.01, 0.012, 0.011])  # RSI(2) pinned high
        card = make_strategy().emit_signal_card("TLT", StubAdapter(df), self._position())
        assert card.signal_type == "SELL_EQUITY"
        assert card.legs[0].side == "sell"
        assert card.legs[0].qty == 12

    def test_sell_on_max_hold_days(self):
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=10)).isoformat()
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])  # RSI still low
        card = make_strategy().emit_signal_card("TLT", StubAdapter(df), self._position(old))
        assert card.signal_type == "SELL_EQUITY"
        assert "max_hold_days" in card.confidence_rationale

    def test_hold_when_no_exit_rule_fires(self):
        from datetime import date
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])
        card = make_strategy().emit_signal_card(
            "TLT", StubAdapter(df), self._position(date.today().isoformat()))
        assert card.signal_type == "NO_SIGNAL"
        assert "holding_position" in card.no_signal_reason
        assert card.payload["days_held"] == 0


# ---------------------------------------------------------------------------
# simulate_trade
# ---------------------------------------------------------------------------

class TestSimulateTrade:
    def _card(self, df: pd.DataFrame):
        return make_strategy().emit_signal_card("TLT", StubAdapter(df), {})

    def test_rsi_exit_positive_pnl(self):
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])
        card = self._card(df)
        assert card.signal_type == "BUY_EQUITY"
        # Future: strong bounce → RSI(2) recovers fast
        entry = float(df["close"].iloc[-1])
        future_closes = [entry * 1.02, entry * 1.045, entry * 1.05]
        fut = pd.DataFrame({
            "open": future_closes, "high": future_closes, "low": future_closes,
            "close": future_closes, "volume": [1e6] * 3,
        }, index=pd.bdate_range(start="2026-05-18", periods=3, tz="UTC"))
        result = make_strategy().simulate_trade(card, fut)
        assert result.pnl > 0
        assert result.metadata["outcome"] in ("RSI_EXIT", "MAX_HOLD_EXIT")

    def test_max_hold_exit(self):
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])
        card = self._card(df)
        entry = float(df["close"].iloc[-1])
        # Keeps drifting down → RSI never recovers → exit at bar 5
        future_closes = [entry * (1 - 0.002 * i) for i in range(1, 9)]
        fut = pd.DataFrame({
            "open": future_closes, "high": future_closes, "low": future_closes,
            "close": future_closes, "volume": [1e6] * 8,
        }, index=pd.bdate_range(start="2026-05-18", periods=8, tz="UTC"))
        result = make_strategy().simulate_trade(card, fut)
        assert result.metadata["outcome"] == "MAX_HOLD_EXIT"
        assert result.exit_date == fut.index[4].date()  # 5th bar
        assert result.pnl < 0

    def test_empty_future_returns_open(self):
        df = make_ohlcv(tail_rets=[-0.012, -0.015, -0.011])
        card = self._card(df)
        result = make_strategy().simulate_trade(card, pd.DataFrame())
        assert result.pnl == 0.0
        assert result.metadata["outcome"] == "OPEN"


# ---------------------------------------------------------------------------
# Registry / router / state
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_registered_in_registry(self):
        import src.strategies  # noqa: F401 — trigger registration
        from src.strategies.registry import list_strategies
        assert "rsi2" in list_strategies()

    def test_router_builds_rsi2_from_project_config(self):
        import src.strategies  # noqa: F401
        from src.router import build_strategy, load_router, resolve_strategy
        cfg = load_router("config/strategies.toml")
        assert resolve_strategy("TLT", cfg) == "rsi2"
        strategy = build_strategy("rsi2", cfg)
        assert strategy.name == "rsi2"
        assert strategy.get_state_schema() is RSI2State

    def test_fixture_adapter_end_to_end(self):
        from src.adapters.fixture_adapter import FixtureAdapter
        s = make_strategy()
        card = s.emit_signal_card("TLT", FixtureAdapter(), {})
        assert card.signal_type == "BUY_EQUITY"
        assert card.legs[0].qty >= 1

    def test_state_from_dict_ignores_unknown_keys(self):
        st = RSI2State.from_dict({"in_position": True, "shares": 3, "bogus": 1})
        assert st.in_position is True
        assert st.shares == 3
