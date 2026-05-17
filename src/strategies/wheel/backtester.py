"""
Wheel strategy trade simulation helpers (Phase A7).

Provides Black-Scholes pricing, delta-strike search, and simulate_trade
implementation for WheelStrategy. Called by BacktestEngine via the
Strategy.simulate_trade() Protocol.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd

from src.strategies.base import TradeResult


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def realized_vol(closes: pd.Series, window: int = 30) -> float:
    log_ret = closes.pct_change().dropna()
    if len(log_ret) < 5:
        return 0.25
    return float(log_ret.tail(window).std() * math.sqrt(252))


def norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def bs_price(flag: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes option price. flag='p' for put, 'c' for call."""
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-4)
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if flag == "p":
            return max(0.0, K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1))
        return max(0.0, S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2))
    except Exception:
        return max(0.0, (K - S) if flag == "p" else (S - K))


def bs_delta(flag: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-4)
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return norm_cdf(d1) - 1.0 if flag == "p" else norm_cdf(d1)
    except Exception:
        return 0.0


def find_delta_strike(
    flag: str, S: float, T: float, r: float, sigma: float, target_delta: float
) -> float:
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-4)
    lo, hi = S * 0.50, S * 1.50
    mid = S
    for _ in range(60):
        mid = (lo + hi) / 2.0
        try:
            d = abs(bs_delta(flag, S, mid, T, r, sigma))
        except Exception:
            return round(S * (0.93 if flag == "p" else 1.07), 2)
        if abs(d - target_delta) < 0.001:
            break
        if flag == "p":
            if d < target_delta:
                lo = mid
            else:
                hi = mid
        else:
            if d < target_delta:
                hi = mid
            else:
                lo = mid
    return round(mid, 2)


# ---------------------------------------------------------------------------
# Wheel-specific simulate_trade implementation
# ---------------------------------------------------------------------------

def simulate_wheel_trade(
    signal_type: str,
    ticker: str,
    strike: float,
    entry_date: date,
    dte_target: int,
    entry_ohlcv: pd.DataFrame,
    future_ohlcv: pd.DataFrame,
    r_free: float = 0.05,
) -> TradeResult:
    """
    Simulate a single Wheel trade from entry to expiry using BS pricing.

    signal_type: "SELL_PUT" or "SELL_CALL"
    strike: recommended strike from the signal card
    entry_ohlcv: history up to (and including) entry_date
    future_ohlcv: OHLCV data from entry_date onward (used to get expiry price)
    """
    flag = "p" if signal_type == "SELL_PUT" else "c"
    S = float(entry_ohlcv["close"].iloc[-1])
    sigma = realized_vol(entry_ohlcv["close"])
    T = dte_target / 365.0
    premium = bs_price(flag, S, strike, T, r_free, sigma)
    expiry_date = entry_date + timedelta(days=dte_target)

    expiry_ts = pd.Timestamp(expiry_date, tz="UTC")
    future_at_expiry = future_ohlcv[future_ohlcv.index >= expiry_ts]

    if future_at_expiry.empty:
        pnl = 0.0
        outcome = "OPEN"
    else:
        exp_price = float(future_at_expiry["close"].iloc[0])
        assigned = (exp_price < strike) if flag == "p" else (exp_price > strike)
        if assigned:
            pnl = premium - abs(strike - exp_price)
            outcome = "ASSIGNED"
        else:
            pnl = premium
            outcome = "EXPIRED_WORTHLESS"

    return TradeResult(
        entry_date=entry_date,
        exit_date=expiry_date,
        signal_type=signal_type,
        ticker=ticker,
        pnl=round(pnl, 2),
        metadata={
            "strike": round(strike, 2),
            "premium": round(premium, 2),
            "iv_at_entry": round(sigma, 4),
            "outcome": outcome,
        },
    )
