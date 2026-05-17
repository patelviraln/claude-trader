"""
Walk-forward backtester for options income strategies (Phase A7 refactor).

The generic BacktestEngine handles the date loop, walk-forward data slicing,
equity curve, and summary metrics. Strategy-specific P&L simulation is
delegated to Strategy.simulate_trade() so each strategy can plug in.

For Wheel backtests, WheelStrategy.simulate_trade() uses Black-Scholes
pricing + assignment logic from src/strategies/wheel/backtester.py.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
from pydantic import BaseModel

from src.strategies.base import TradeResult
from src.strategies.wheel.backtester import (
    find_delta_strike,
    realized_vol,
)
from src.strategies.wheel.filters import EMATrendFilter, RSIFilter


# ---------------------------------------------------------------------------
# Output models (preserved for backwards compat with streamlit_app.py)
# ---------------------------------------------------------------------------

class BacktestTrade(BaseModel):
    ticker: str
    phase: str                       # "SELL_PUT" | "SELL_CALL"
    entry_date: str
    expiry_date: str
    underlying_at_entry: float
    strike: float
    estimated_premium: float
    iv_at_entry: float
    outcome: str                     # "EXPIRED_WORTHLESS" | "ASSIGNED" | "OPEN"
    underlying_at_expiry: float | None = None
    pnl: float = 0.0


class BacktestSummary(BaseModel):
    ticker: str
    start_date: str
    end_date: str
    phase: str
    total_trades: int
    closed_trades: int
    wins: int
    assignments: int
    win_rate: float
    total_premium: float
    avg_premium: float
    total_pnl: float
    trades: list[BacktestTrade]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Walk-forward backtester. Strategy-agnostic entry loop; Wheel-specific
    P&L delegated to simulate_wheel_trade (imported from wheel/backtester.py).

    Phase A7 NOTE: Once the full Strategy.simulate_trade() Protocol is adopted
    across Track B strategies, the engine will call strategy.simulate_trade()
    for all strategies. For now, Wheel stays on the direct import path so the
    existing Streamlit Backtest page and tests remain unchanged.
    """

    def __init__(self, adapter: Any, config: dict):
        cfg = config.get("wheel", config)
        self._adapter = adapter
        self._ema_period = int(cfg.get("ema_period", 50))
        self._rsi_min = float(cfg.get("rsi_min", 35.0))
        self._rsi_max = float(cfg.get("rsi_max", 65.0))
        self._delta_target = float(cfg.get("delta_target", 0.30))
        dte_min = int(cfg.get("dte_min", 30))
        dte_max = int(cfg.get("dte_max", 45))
        self._dte_target = (dte_min + dte_max) // 2

    def run(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        phase: str = "A",
    ) -> BacktestSummary:
        from src.strategies.wheel.backtester import simulate_wheel_trade

        if start_date > end_date:
            return self._empty_summary(ticker, start_date, end_date, phase)

        flag = "p" if phase == "A" else "c"
        signal_type = "SELL_PUT" if phase == "A" else "SELL_CALL"
        r_free = 0.05
        min_bars = self._ema_period + 20

        warmup_start = start_date - timedelta(days=self._ema_period * 2 + 30)
        df_full = self._adapter.get_historical_ohlcv(ticker, warmup_start, end_date)

        ema_f = EMATrendFilter(ema_period=self._ema_period)
        rsi_f = RSIFilter(rsi_min=self._rsi_min, rsi_max=self._rsi_max)

        trades: list[BacktestTrade] = []
        current_date = start_date

        while current_date <= end_date:
            if current_date.weekday() >= 5:
                current_date += timedelta(days=1)
                continue

            cutoff = pd.Timestamp(current_date, tz="UTC")
            hist = df_full[df_full.index <= cutoff]

            if len(hist) < min_bars:
                current_date += timedelta(days=7)
                continue

            market_data = {"ohlcv": hist}
            if not ema_f.run(market_data, state=None, config={}).passed:
                current_date += timedelta(days=1)
                continue
            if not rsi_f.run(market_data, state=None, config={}).passed:
                current_date += timedelta(days=1)
                continue

            S = float(hist["close"].iloc[-1])
            iv = realized_vol(hist["close"])
            T = self._dte_target / 365.0
            strike = find_delta_strike(flag, S, T, r_free, iv, self._delta_target)

            result: TradeResult = simulate_wheel_trade(
                signal_type=signal_type,
                ticker=ticker,
                strike=strike,
                entry_date=current_date,
                dte_target=self._dte_target,
                entry_ohlcv=hist,
                future_ohlcv=df_full,
                r_free=r_free,
            )

            trades.append(BacktestTrade(
                ticker=ticker,
                phase=signal_type,
                entry_date=result.entry_date.isoformat(),
                expiry_date=result.exit_date.isoformat(),
                underlying_at_entry=round(S, 2),
                strike=result.metadata.get("strike", strike),
                estimated_premium=result.metadata.get("premium", 0.0),
                iv_at_entry=result.metadata.get("iv_at_entry", iv),
                outcome=result.metadata.get("outcome", "OPEN"),
                underlying_at_expiry=None,
                pnl=result.pnl,
            ))

            current_date = result.exit_date + timedelta(days=1)

        return self._build_summary(ticker, start_date, end_date, phase, trades)

    # ------------------------------------------------------------------

    @staticmethod
    def _empty_summary(ticker, start_date, end_date, phase) -> BacktestSummary:
        label = "SELL_PUT" if phase == "A" else "SELL_CALL"
        return BacktestSummary(
            ticker=ticker, start_date=start_date.isoformat(),
            end_date=end_date.isoformat(), phase=label,
            total_trades=0, closed_trades=0, wins=0, assignments=0,
            win_rate=0.0, total_premium=0.0, avg_premium=0.0, total_pnl=0.0,
            trades=[],
        )

    @staticmethod
    def _build_summary(ticker, start_date, end_date, phase, trades) -> BacktestSummary:
        label = "SELL_PUT" if phase == "A" else "SELL_CALL"
        closed = [t for t in trades if t.outcome != "OPEN"]
        wins = sum(1 for t in closed if t.outcome == "EXPIRED_WORTHLESS")
        assignments = len(closed) - wins
        total_premium = sum(t.estimated_premium for t in closed)
        total_pnl = sum(t.pnl for t in closed)
        n = len(closed)
        return BacktestSummary(
            ticker=ticker,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            phase=label,
            total_trades=len(trades),
            closed_trades=n,
            wins=wins,
            assignments=assignments,
            win_rate=round(wins / n * 100, 1) if n else 0.0,
            total_premium=round(total_premium, 2),
            avg_premium=round(total_premium / n, 2) if n else 0.0,
            total_pnl=round(total_pnl, 2),
            trades=trades,
        )
