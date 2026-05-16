"""Unit tests for src/backtester.py."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.backtester import (
    BacktestEngine,
    BacktestSummary,
    _bs_price,
    _find_delta_strike,
    _realized_vol,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(
    n_days: int,
    start_price: float = 150.0,
    drift: float = 0.0008,
    vol: float = 0.012,
    start: str = "2024-01-01",
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic OHLCV with a noisy upward random walk; UTC DatetimeIndex."""
    rng = np.random.default_rng(seed=seed)
    dates = pd.bdate_range(start=start, periods=n_days, freq="B", tz="UTC")
    daily_ret = rng.normal(drift, vol, n_days)
    closes = start_price * np.exp(np.cumsum(daily_ret))
    data = {
        "open":   closes * rng.uniform(0.997, 1.003, n_days),
        "high":   closes * rng.uniform(1.002, 1.014, n_days),
        "low":    closes * rng.uniform(0.986, 0.998, n_days),
        "close":  closes,
        "volume": rng.integers(800_000, 5_000_000, n_days).astype(float),
    }
    return pd.DataFrame(np.round(data, 2) if False else data, index=dates)


class _MockAdapter:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_historical_ohlcv(self, ticker, start_date, end_date):
        return self._df


_DEFAULT_CONFIG = {
    "wheel": {
        "ema_period": 10,
        "rsi_min": 20.0,
        "rsi_max": 80.0,
        "delta_target": 0.30,
        "dte_min": 21,
        "dte_max": 35,
    }
}


# ---------------------------------------------------------------------------
# _realized_vol
# ---------------------------------------------------------------------------

class TestRealizedVol:
    def test_returns_float(self):
        closes = pd.Series([100.0 + i for i in range(50)])
        result = _realized_vol(closes)
        assert isinstance(result, float)
        assert result >= 0.0

    def test_flat_prices_near_zero(self):
        closes = pd.Series([100.0] * 50)
        assert _realized_vol(closes) < 0.01

    def test_insufficient_data_returns_fallback(self):
        closes = pd.Series([100.0, 101.0, 102.0])
        assert _realized_vol(closes) == 0.25


# ---------------------------------------------------------------------------
# _bs_price
# ---------------------------------------------------------------------------

class TestBsPrice:
    def test_put_price_positive(self):
        price = _bs_price("p", 150.0, 145.0, 30 / 365, 0.05, 0.25)
        assert price > 0.0

    def test_call_price_positive(self):
        price = _bs_price("c", 150.0, 155.0, 30 / 365, 0.05, 0.25)
        assert price > 0.0

    def test_deep_itm_put_price_nonzero(self):
        price = _bs_price("p", 100.0, 130.0, 30 / 365, 0.05, 0.25)
        assert price >= 30.0 * 0.9  # near intrinsic

    def test_handles_zero_time(self):
        # T = 0 should not crash, returns max(intrinsic, 0)
        price = _bs_price("p", 100.0, 95.0, 0.0, 0.05, 0.25)
        assert price >= 0.0


# ---------------------------------------------------------------------------
# _find_delta_strike
# ---------------------------------------------------------------------------

class TestFindDeltaStrike:
    def test_put_strike_below_spot(self):
        strike = _find_delta_strike("p", 150.0, 30 / 365, 0.05, 0.25, 0.30)
        assert strike < 150.0

    def test_call_strike_above_spot(self):
        strike = _find_delta_strike("c", 150.0, 30 / 365, 0.05, 0.25, 0.30)
        assert strike > 150.0

    def test_put_strike_reasonable_range(self):
        strike = _find_delta_strike("p", 150.0, 30 / 365, 0.05, 0.25, 0.30)
        assert 100.0 < strike < 150.0

    def test_call_strike_reasonable_range(self):
        strike = _find_delta_strike("c", 150.0, 30 / 365, 0.05, 0.25, 0.30)
        assert 150.0 < strike < 200.0


# ---------------------------------------------------------------------------
# BacktestEngine.run — basic scenarios
# ---------------------------------------------------------------------------

class TestBacktestEngineRun:
    def test_empty_range_returns_no_trades(self):
        df = _make_ohlcv(200)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2025, 6, 1), date(2025, 5, 1))
        assert result.total_trades == 0
        assert result.win_rate == 0.0

    def test_insufficient_warmup_skips_gracefully(self):
        df = _make_ohlcv(5)  # far too few bars
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 1, 2), date(2024, 1, 31))
        assert isinstance(result, BacktestSummary)

    def test_uptrend_generates_trades(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 12, 31))
        assert result.total_trades > 0

    def test_uptrend_all_puts_expire_worthless(self):
        df = _make_ohlcv(300, start_price=150.0, drift=0.002, vol=0.005)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 10, 31))
        closed = [t for t in result.trades if t.outcome != "OPEN"]
        if closed:
            assert all(t.outcome == "EXPIRED_WORTHLESS" for t in closed)

    def test_last_trade_may_be_open(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 12, 20))
        if result.total_trades > 0:
            # Last trade near end_date may be OPEN
            assert result.trades[-1].outcome in ("EXPIRED_WORTHLESS", "ASSIGNED", "OPEN")

    def test_phase_b_produces_sell_call_trades(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 10, 31), phase="B")
        for t in result.trades:
            assert t.phase == "SELL_CALL"

    def test_phase_a_produces_sell_put_trades(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 10, 31), phase="A")
        for t in result.trades:
            assert t.phase == "SELL_PUT"

    def test_summary_fields_populated(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 10, 31))
        assert result.ticker == "TEST"
        assert result.start_date == "2024-03-01"
        assert result.end_date == "2024-10-31"
        assert result.phase == "SELL_PUT"

    def test_win_rate_between_0_and_100(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 10, 31))
        assert 0.0 <= result.win_rate <= 100.0

    def test_total_premium_positive_when_trades_exist(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 10, 31))
        if result.closed_trades > 0:
            assert result.total_premium > 0.0

    def test_closed_trades_lte_total_trades(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 12, 20))
        assert result.closed_trades <= result.total_trades

    def test_wins_plus_assignments_equals_closed(self):
        df = _make_ohlcv(300)
        engine = BacktestEngine(_MockAdapter(df), _DEFAULT_CONFIG)
        result = engine.run("TEST", date(2024, 3, 1), date(2024, 10, 31))
        assert result.wins + result.assignments == result.closed_trades
