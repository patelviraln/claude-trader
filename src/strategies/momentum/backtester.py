"""
RSI(2) trade simulation: bar-by-bar equity P&L from entry until exit signal
or max_hold_days, with commission and half-spread slippage subtracted.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import ta.momentum as momentum_ind

from src.strategies.base import TradeResult


def simulate_rsi2_trade(
    ticker: str,
    entry_date: date,
    entry_price: float,
    shares: int,
    future_ohlcv: pd.DataFrame,
    trailing_closes: pd.Series | None = None,
    exit_rsi_min: float = 70.0,
    max_hold_days: int = 5,
    rsi_period: int = 2,
    commission_per_share: float = 0.0,
    slippage_pct: float = 0.05,
) -> TradeResult:
    """
    Walk future bars: exit at the first close where RSI(rsi_period) > exit_rsi_min,
    or at the max_hold_days-th bar, whichever comes first.

    trailing_closes: closes up to and including entry — needed so RSI has
    warm-up history on the first future bars. P&L = (exit - entry) * shares,
    minus commission both ways and half-spread slippage on both fills.
    """
    if future_ohlcv.empty or shares <= 0:
        return TradeResult(
            entry_date=entry_date,
            exit_date=entry_date,
            signal_type="BUY_EQUITY",
            ticker=ticker,
            pnl=0.0,
            metadata={"outcome": "OPEN", "entry_price": round(entry_price, 4), "shares": shares},
        )

    history = trailing_closes if trailing_closes is not None else pd.Series(dtype=float)

    exit_price: float | None = None
    exit_ts = future_ohlcv.index[-1]
    outcome = "OPEN"

    for i, (ts, bar) in enumerate(future_ohlcv.iterrows(), start=1):
        closes = pd.concat([history, future_ohlcv["close"].iloc[:i]])
        rsi_val = float(
            momentum_ind.RSIIndicator(close=closes, window=rsi_period).rsi().iloc[-1]
        )
        if rsi_val > exit_rsi_min:
            exit_price, exit_ts, outcome = float(bar["close"]), ts, "RSI_EXIT"
            break
        if i >= max_hold_days:
            exit_price, exit_ts, outcome = float(bar["close"]), ts, "MAX_HOLD_EXIT"
            break

    if exit_price is None:
        exit_price, outcome = float(future_ohlcv["close"].iloc[-1]), "MAX_HOLD_EXIT"

    slip = slippage_pct / 100.0
    fill_entry = entry_price * (1 + slip)
    fill_exit = exit_price * (1 - slip)
    costs = commission_per_share * shares * 2
    pnl = (fill_exit - fill_entry) * shares - costs

    return TradeResult(
        entry_date=entry_date,
        exit_date=exit_ts.date() if hasattr(exit_ts, "date") else entry_date,
        signal_type="BUY_EQUITY",
        ticker=ticker,
        pnl=round(pnl, 2),
        metadata={
            "outcome": outcome,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "shares": shares,
            "costs": round(costs + (entry_price + exit_price) * slip * shares, 4),
        },
    )
