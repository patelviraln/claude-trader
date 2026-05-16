"""
Indicator computation using the 'ta' library (Technical Analysis for Python).
All functions accept a pandas DataFrame with OHLCV columns and return typed dicts.
"""
from typing import TypedDict

import pandas as pd
import structlog
import ta.trend as trend_ind
import ta.momentum as momentum_ind
import ta.volatility as vol_ind

import src.logger as _log_setup  # noqa: F401

logger = structlog.get_logger(__name__)


class EMAResult(TypedDict):
    ema: float
    slope: float  # 5-bar linear slope (price units / bar)


class RSIResult(TypedDict):
    rsi: float


class BollingerResult(TypedDict):
    upper: float
    mid: float
    lower: float
    percent_b: float  # 0 = at lower band, 1 = at upper band


class VolumeProfileResult(TypedDict):
    hvn_price: float           # price of highest-volume node
    hvn_type: str              # "support" | "resistance" relative to current close
    histogram: dict[float, float]  # price_level -> volume


def compute_ema(df: pd.DataFrame, period: int) -> EMAResult:
    """Compute EMA and 5-bar slope on the close column."""
    ema_series = trend_ind.EMAIndicator(close=df["close"], window=period).ema_indicator()
    ema_val = float(ema_series.iloc[-1])

    window = ema_series.iloc[-5:]
    slope = float((window.iloc[-1] - window.iloc[0]) / (len(window) - 1)) if len(window) >= 2 else 0.0

    logger.debug("indicator_computed", indicator="ema", ema=round(ema_val, 4), slope=round(slope, 6))
    return EMAResult(ema=ema_val, slope=slope)


def compute_rsi(df: pd.DataFrame, period: int = 14) -> RSIResult:
    """Compute RSI on the close column."""
    rsi_series = momentum_ind.RSIIndicator(close=df["close"], window=period).rsi()
    rsi_val = float(rsi_series.iloc[-1])
    logger.debug("indicator_computed", indicator="rsi", rsi=round(rsi_val, 2))
    return RSIResult(rsi=rsi_val)


def compute_bollinger(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> BollingerResult:
    """Compute Bollinger Bands and %B on the close column."""
    bb = vol_ind.BollingerBands(close=df["close"], window=period, window_dev=std_dev)
    upper = float(bb.bollinger_hband().iloc[-1])
    mid = float(bb.bollinger_mavg().iloc[-1])
    lower = float(bb.bollinger_lband().iloc[-1])
    pct_b = float(bb.bollinger_pband().iloc[-1])  # (close - lower) / (upper - lower)

    logger.debug("indicator_computed", indicator="bollinger", percent_b=round(pct_b, 4))
    return BollingerResult(upper=upper, mid=mid, lower=lower, percent_b=pct_b)


def compute_iv_rank(current_iv: float, iv_series: list[float]) -> float | None:
    """IV rank (0-100): where current IV sits within the historical high/low range."""
    if len(iv_series) < 2:
        return None
    iv_low = min(iv_series)
    iv_high = max(iv_series)
    if iv_high == iv_low:
        return 50.0
    return round((current_iv - iv_low) / (iv_high - iv_low) * 100, 1)


def compute_volume_profile(df: pd.DataFrame, lookback: int = 20) -> VolumeProfileResult:
    """
    Compute a price-volume histogram over the last `lookback` bars.
    Bins prices to nearest 0.50 increments. Returns the highest-volume node (HVN).
    """
    recent = df.iloc[-lookback:].copy()
    typical = (recent["high"] + recent["low"] + recent["close"]) / 3

    bin_size = 0.5
    price_bins = (typical / bin_size).round() * bin_size

    histogram: dict[float, float] = {}
    for price, volume in zip(price_bins, recent["volume"]):
        p = round(float(price), 2)
        histogram[p] = histogram.get(p, 0.0) + float(volume)

    hvn_price = max(histogram, key=lambda p: histogram[p])
    current_close = float(df["close"].iloc[-1])
    hvn_type = "support" if hvn_price <= current_close else "resistance"

    logger.debug("indicator_computed", indicator="volume_profile", hvn_price=hvn_price, hvn_type=hvn_type)
    return VolumeProfileResult(hvn_price=hvn_price, hvn_type=hvn_type, histogram=histogram)
