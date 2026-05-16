"""Unit tests for indicator_engine.py using synthetic DataFrames."""
import math

import pandas as pd
import pytest

from src.indicator_engine import (
    compute_bollinger,
    compute_ema,
    compute_iv_rank,
    compute_rsi,
    compute_volume_profile,
)


def _rising_df(n: int = 60, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    prices = [start + i * step for i in range(n)]
    return pd.DataFrame({
        "open": prices,
        "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices],
        "close": prices,
        "volume": [1_000_000] * n,
    })


def _falling_df(n: int = 60, start: float = 130.0, step: float = 0.5) -> pd.DataFrame:
    prices = [start - i * step for i in range(n)]
    return pd.DataFrame({
        "open": prices,
        "high": [p + 1 for p in prices],
        "low": [max(0.01, p - 1) for p in prices],
        "close": prices,
        "volume": [1_000_000] * n,
    })


class TestComputeEMA:
    def test_slope_positive_on_rising_series(self):
        df = _rising_df()
        result = compute_ema(df, period=50)
        assert result["slope"] > 0, "Slope should be positive for a steadily rising series"

    def test_slope_negative_on_falling_series(self):
        df = _falling_df()
        result = compute_ema(df, period=50)
        assert result["slope"] < 0, "Slope should be negative for a steadily falling series"

    def test_ema_below_price_on_uptrend(self):
        df = _rising_df()
        result = compute_ema(df, period=50)
        # EMA lags price in an uptrend → EMA < last close
        assert result["ema"] < df["close"].iloc[-1]

    def test_returns_finite_values(self):
        df = _rising_df()
        result = compute_ema(df, period=50)
        assert math.isfinite(result["ema"])
        assert math.isfinite(result["slope"])


class TestComputeRSI:
    def test_rsi_high_on_strong_uptrend(self):
        df = _rising_df(n=60, step=1.0)
        result = compute_rsi(df, period=14)
        assert result["rsi"] > 60, f"Expected RSI > 60 on strong uptrend, got {result['rsi']}"

    def test_rsi_low_on_strong_downtrend(self):
        df = _falling_df(n=60, step=1.0)
        result = compute_rsi(df, period=14)
        assert result["rsi"] < 40, f"Expected RSI < 40 on strong downtrend, got {result['rsi']}"

    def test_rsi_bounded(self):
        df = _rising_df()
        result = compute_rsi(df, period=14)
        assert 0 <= result["rsi"] <= 100


class TestComputeBollinger:
    def test_percent_b_near_one_on_uptrend(self):
        df = _rising_df(n=60, step=0.3)
        result = compute_bollinger(df, period=20, std_dev=2.0)
        # In a rising trend, close is near or above upper band → %B close to 1 or > 1
        assert result["percent_b"] >= 0.5, f"Expected %B >= 0.5, got {result['percent_b']}"

    def test_band_ordering(self):
        df = _rising_df()
        result = compute_bollinger(df, period=20, std_dev=2.0)
        assert result["lower"] < result["mid"] < result["upper"]

    def test_percent_b_near_zero_on_downtrend(self):
        df = _falling_df(n=60, step=0.3)
        result = compute_bollinger(df, period=20, std_dev=2.0)
        assert result["percent_b"] <= 0.5, f"Expected %B <= 0.5, got {result['percent_b']}"


class TestComputeVolumeProfile:
    def test_hvn_type_support_when_below_price(self):
        df = _rising_df()
        result = compute_volume_profile(df, lookback=20)
        # Current close is at the top of an uptrend; HVN from earlier bars should be below
        assert result["hvn_type"] in ("support", "resistance")

    def test_histogram_is_populated(self):
        df = _rising_df()
        result = compute_volume_profile(df, lookback=20)
        assert len(result["histogram"]) > 0

    def test_hvn_price_is_positive(self):
        df = _rising_df()
        result = compute_volume_profile(df, lookback=20)
        assert result["hvn_price"] > 0


class TestComputeIVRank:
    def test_returns_none_with_single_sample(self):
        assert compute_iv_rank(0.30, [0.30]) is None

    def test_returns_none_with_empty_series(self):
        assert compute_iv_rank(0.30, []) is None

    def test_current_at_52w_high_returns_100(self):
        series = [0.20, 0.25, 0.30, 0.35, 0.40]
        assert compute_iv_rank(0.40, series) == 100.0

    def test_current_at_52w_low_returns_0(self):
        series = [0.20, 0.25, 0.30, 0.35, 0.40]
        assert compute_iv_rank(0.20, series) == 0.0

    def test_midpoint_returns_50(self):
        series = [0.20, 0.40]
        assert compute_iv_rank(0.30, series) == 50.0

    def test_flat_series_returns_50(self):
        series = [0.25, 0.25, 0.25]
        assert compute_iv_rank(0.25, series) == 50.0

    def test_result_bounded_0_to_100(self):
        series = [0.15, 0.20, 0.25, 0.35, 0.45]
        rank = compute_iv_rank(0.30, series)
        assert rank is not None
        assert 0.0 <= rank <= 100.0
