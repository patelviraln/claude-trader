"""
Phase 6 — Walk-forward backtester for the Wheel strategy.

Uses 30-day realized volatility + Black-Scholes to simulate options entries
on each day the EMA and RSI hard filters pass, then checks outcome at expiry.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import pandas as pd
from pydantic import BaseModel

from src.strategies.wheel.filters import EMATrendFilter, RSIFilter


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class BacktestTrade(BaseModel):
    ticker: str
    phase: str                       # "SELL_PUT" | "SELL_CALL"
    entry_date: str
    expiry_date: str
    underlying_at_entry: float
    strike: float
    estimated_premium: float         # per share
    iv_at_entry: float
    outcome: str                     # "EXPIRED_WORTHLESS" | "ASSIGNED" | "OPEN"
    underlying_at_expiry: float | None = None
    pnl: float = 0.0                 # per share


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
# Math helpers
# ---------------------------------------------------------------------------

def _realized_vol(closes: pd.Series, window: int = 30) -> float:
    log_ret = closes.pct_change().dropna()
    if len(log_ret) < 5:
        return 0.25
    return float(log_ret.tail(window).std() * math.sqrt(252))


def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _bs_price(flag: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-4)
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if flag == "p":
            return max(0.0, K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1))
        return max(0.0, S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2))
    except Exception:
        return max(0.0, (K - S) if flag == "p" else (S - K))


def _bs_delta(flag: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-4)
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return _norm_cdf(d1) - 1.0 if flag == "p" else _norm_cdf(d1)
    except Exception:
        return 0.0


def _find_delta_strike(
    flag: str, S: float, T: float, r: float, sigma: float, target_delta: float
) -> float:
    T = max(T, 1e-6)
    sigma = max(sigma, 1e-4)
    lo, hi = S * 0.50, S * 1.50
    mid = S
    for _ in range(60):
        mid = (lo + hi) / 2.0
        try:
            d = abs(_bs_delta(flag, S, mid, T, r, sigma))
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
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
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
        if start_date > end_date:
            return self._empty_summary(ticker, start_date, end_date, phase)

        flag = "p" if phase == "A" else "c"
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

            if not ema_f.run(hist).passed:
                current_date += timedelta(days=1)
                continue
            if not rsi_f.run(hist).passed:
                current_date += timedelta(days=1)
                continue

            S = float(hist["close"].iloc[-1])
            iv = _realized_vol(hist["close"])
            T = self._dte_target / 365.0

            strike = _find_delta_strike(flag, S, T, r_free, iv, self._delta_target)
            premium = _bs_price(flag, S, strike, T, r_free, iv)

            expiry_date = current_date + timedelta(days=self._dte_target)

            expiry_ts = pd.Timestamp(expiry_date, tz="UTC")
            future = df_full[df_full.index >= expiry_ts]

            if future.empty or expiry_date > end_date:
                outcome = "OPEN"
                underlying_at_expiry = None
                pnl = 0.0
            else:
                exp_price = float(future["close"].iloc[0])
                assigned = (exp_price < strike) if phase == "A" else (exp_price > strike)
                outcome = "ASSIGNED" if assigned else "EXPIRED_WORTHLESS"
                pnl = premium - abs(strike - exp_price) if assigned else premium
                underlying_at_expiry = exp_price

            trades.append(BacktestTrade(
                ticker=ticker,
                phase="SELL_PUT" if phase == "A" else "SELL_CALL",
                entry_date=current_date.isoformat(),
                expiry_date=expiry_date.isoformat(),
                underlying_at_entry=round(S, 2),
                strike=round(strike, 2),
                estimated_premium=round(premium, 2),
                iv_at_entry=round(iv, 4),
                outcome=outcome,
                underlying_at_expiry=round(underlying_at_expiry, 2) if underlying_at_expiry is not None else None,
                pnl=round(pnl, 2),
            ))

            current_date = expiry_date + timedelta(days=1)

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
