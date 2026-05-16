from typing import TypedDict

import pandas as pd
import pandas_ta as ta


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
    hvn_price: float          # price level of highest-volume node
    hvn_type: str             # "support" | "resistance" relative to current close
    histogram: dict[float, float]  # price_level -> volume


def compute_ema(df: pd.DataFrame, period: int) -> EMAResult:
    """Compute EMA and 5-bar slope on the close column."""
    ema_series: pd.Series = ta.ema(df["close"], length=period)
    ema_val = float(ema_series.iloc[-1])

    # Linear slope over last 5 bars (rise / run in price-per-bar units)
    window = ema_series.iloc[-5:]
    slope = float((window.iloc[-1] - window.iloc[0]) / (len(window) - 1)) if len(window) >= 2 else 0.0

    return EMAResult(ema=ema_val, slope=slope)


def compute_rsi(df: pd.DataFrame, period: int = 14) -> RSIResult:
    """Compute RSI on the close column."""
    rsi_series: pd.Series = ta.rsi(df["close"], length=period)
    return RSIResult(rsi=float(rsi_series.iloc[-1]))


def compute_bollinger(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> BollingerResult:
    """Compute Bollinger Bands and %B on the close column."""
    bb = ta.bbands(df["close"], length=period, std=std_dev)
    # pandas-ta column naming: BBL_{period}_{std}, BBM_{period}_{std}, BBU_{period}_{std}, BBB_{period}_{std}, BBP_{period}_{std}
    std_str = str(std_dev) if "." in str(std_dev) else f"{std_dev}.0"
    lower = float(bb[f"BBL_{period}_{std_str}"].iloc[-1])
    mid = float(bb[f"BBM_{period}_{std_str}"].iloc[-1])
    upper = float(bb[f"BBU_{period}_{std_str}"].iloc[-1])

    band_width = upper - lower
    close = float(df["close"].iloc[-1])
    percent_b = (close - lower) / band_width if band_width > 0 else 0.5

    return BollingerResult(upper=upper, mid=mid, lower=lower, percent_b=percent_b)


def compute_volume_profile(df: pd.DataFrame, lookback: int = 20) -> VolumeProfileResult:
    """
    Compute a simple price-volume histogram over the last `lookback` bars.
    Bins are determined by rounding each bar's typical price to 2 decimal places.
    Returns the highest-volume node (HVN) and its type relative to current close.
    """
    recent = df.iloc[-lookback:].copy()
    typical = (recent["high"] + recent["low"] + recent["close"]) / 3

    # Round to nearest 0.5 to create meaningful price bins
    bin_size = 0.5
    price_bins = (typical / bin_size).round() * bin_size

    histogram: dict[float, float] = {}
    for price, volume in zip(price_bins, recent["volume"]):
        p = round(float(price), 2)
        histogram[p] = histogram.get(p, 0.0) + float(volume)

    hvn_price = max(histogram, key=lambda p: histogram[p])
    current_close = float(df["close"].iloc[-1])
    hvn_type = "support" if hvn_price <= current_close else "resistance"

    return VolumeProfileResult(hvn_price=hvn_price, hvn_type=hvn_type, histogram=histogram)
