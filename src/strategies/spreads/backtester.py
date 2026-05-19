"""
Put Credit Spread trade simulation helpers.

Simulates a bull put spread (sell short put, buy long put) from entry to expiry.
Uses the same Black-Scholes helpers as the Wheel backtester.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from src.strategies.base import TradeResult
from src.strategies.wheel.backtester import bs_price, realized_vol


def simulate_pcs_trade(
    ticker: str,
    short_strike: float,
    long_strike: float,
    entry_date: date,
    dte_target: int,
    net_credit: float,
    entry_ohlcv: pd.DataFrame,
    future_ohlcv: pd.DataFrame,
    r_free: float = 0.05,
) -> TradeResult:
    """
    Simulate a put credit spread from entry to expiry using BS pricing.

    P&L at expiry:
      price > short_strike          → both expire worthless → full credit
      long_strike < price < short_strike → partial loss = short_strike - price - net_credit
      price < long_strike           → max loss = spread_width - net_credit
    All values are per-share (× 100 for one contract).
    """
    spread_width = short_strike - long_strike
    expiry_date = entry_date + timedelta(days=dte_target)

    if future_ohlcv.empty:
        return TradeResult(
            entry_date=entry_date,
            exit_date=expiry_date,
            signal_type="SELL_PUT_SPREAD",
            ticker=ticker,
            pnl=0.0,
            metadata={
                "short_strike": round(short_strike, 2),
                "long_strike": round(long_strike, 2),
                "spread_width": round(spread_width, 2),
                "net_credit": round(net_credit, 4),
                "max_profit": round(net_credit * 100, 2),
                "max_loss": round((spread_width - net_credit) * 100, 2),
                "outcome": "OPEN",
            },
        )

    S = float(entry_ohlcv["close"].iloc[-1]) if not entry_ohlcv.empty else short_strike / 0.95
    sigma = realized_vol(entry_ohlcv["close"]) if not entry_ohlcv.empty else 0.25
    T = dte_target / 365.0

    # Entry credit via BS (override with actual net_credit if provided and positive)
    if net_credit <= 0:
        short_prem = bs_price("p", S, short_strike, T, r_free, sigma)
        long_prem = bs_price("p", S, long_strike, T, r_free, sigma)
        net_credit = max(0.0, short_prem - long_prem)

    expiry_ts = pd.Timestamp(expiry_date, tz="UTC")
    future_at_expiry = future_ohlcv[future_ohlcv.index >= expiry_ts]

    if future_at_expiry.empty:
        pnl = 0.0
        outcome = "OPEN"
    else:
        exp_price = float(future_at_expiry["close"].iloc[0])
        if exp_price >= short_strike:
            pnl = net_credit
            outcome = "EXPIRED_WORTHLESS"
        elif exp_price <= long_strike:
            pnl = net_credit - spread_width  # max loss scenario
            outcome = "MAX_LOSS"
        else:
            # Partial loss: short put is in the money, long put expired worthless
            pnl = net_credit - (short_strike - exp_price)
            outcome = "PARTIAL_LOSS"

    return TradeResult(
        entry_date=entry_date,
        exit_date=expiry_date,
        signal_type="SELL_PUT_SPREAD",
        ticker=ticker,
        pnl=round(pnl * 100, 2),  # one contract = 100 shares
        metadata={
            "short_strike": round(short_strike, 2),
            "long_strike": round(long_strike, 2),
            "spread_width": round(spread_width, 2),
            "net_credit": round(net_credit, 4),
            "max_profit": round(net_credit * 100, 2),
            "max_loss": round((spread_width - net_credit) * 100, 2),
            "iv_at_entry": round(sigma, 4),
            "outcome": outcome,
        },
    )
