"""
RSI2Strategy: Connors RSI(2) mean-reversion on liquid ETFs (Phase B2).

Long-only equity. Entry: close above the 200-day SMA (trend filter) AND
RSI(2) oversold. Exit: RSI(2) recovers above exit_rsi_min OR position held
max_hold_days. One leg per signal — no options, no strikes, no expiries.

Filter chain (cheapest-first):
  PositionGate → TrendFilter (SMA200) → RSIEntryGate → PositionSizer
"""
from __future__ import annotations

import math
from datetime import date
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
from src.indicator_engine import compute_rsi
from src.signal_output import Leg, SignalCard
from src.strategies.base import FilterResult, TradeResult
from src.strategies.momentum.state import RSI2State
from src.strategies.registry import register

if TYPE_CHECKING:
    from src.adapters.base import DataAdapter

logger = structlog.get_logger(__name__)


@register("rsi2")
class RSI2Strategy:
    name = "rsi2"

    def __init__(self):
        self._cfg: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Strategy Protocol
    # ------------------------------------------------------------------

    def load_config(self, config: dict[str, Any]) -> None:
        self._cfg = config

    def get_state_schema(self) -> type[BaseModel]:
        return RSI2State

    def emit_signal_card(
        self,
        ticker: str,
        adapter: "DataAdapter",
        state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> SignalCard:
        trend_ma = int(self._cfg.get("trend_filter_ma", 200))
        bars = max(250, trend_ma + 20)
        ohlcv = adapter.get_ohlcv(ticker, bars=bars)

        results = self.evaluate(ticker, ohlcv, pd.DataFrame(), state)
        indicators = self._collect_adjustments(results).get("indicators", {})
        close = indicators.get("close", float(ohlcv["close"].iloc[-1]))
        rsi2_state = RSI2State.from_dict(state) if state else RSI2State.default()

        # --- In position: exit or hold ---
        if rsi2_state.in_position:
            return self._exit_or_hold_card(ticker, rsi2_state, indicators, close)

        # --- Flat: entry blocked by a hard stop? ---
        stop = next((r for r in results if not r.passed and r.hard_stop), None)
        if stop is not None:
            logger.info("signal_blocked", ticker=ticker, filter=stop.filter_name, reason=stop.reason)
            return SignalCard(
                strategy_name=self.name,
                ticker=ticker,
                signal_type="NO_SIGNAL",
                underlying_price=close,
                indicators=indicators,
                no_signal_reason=f"{stop.filter_name}: {stop.reason}",
            )

        # --- Entry ---
        shares = self._collect_adjustments(results).get("shares", 0)
        rsi_val = indicators.get("rsi2", 100.0)
        tier = "HIGH" if rsi_val < self._cfg.get("entry_rsi_max", 10.0) / 2 else "MEDIUM"
        rationale = f"RSI(2)={rsi_val:.1f} oversold above 200-DMA"

        logger.info("signal_emitted", ticker=ticker, signal_type="BUY_EQUITY",
                    shares=shares, rsi2=round(rsi_val, 2))
        return SignalCard(
            strategy_name=self.name,
            ticker=ticker,
            signal_type="BUY_EQUITY",
            underlying_price=close,
            legs=[Leg(
                asset_class="equity",
                symbol=ticker,
                side="buy",
                qty=shares,
                order_type="limit",
                limit_price=round(close, 2),
            )],
            indicators=indicators,
            payload={"position_value": round(shares * close, 2)},
            confidence_tier=tier,
            confidence_rationale=rationale,
        )

    def evaluate(
        self,
        ticker: str,
        ohlcv: pd.DataFrame,
        options_chain: pd.DataFrame,
        state: dict[str, Any],
    ) -> list[FilterResult]:
        rsi2_state = RSI2State.from_dict(state) if state else RSI2State.default()
        cfg = self._cfg
        trend_ma = int(cfg.get("trend_filter_ma", 200))
        rsi_period = int(cfg.get("rsi_period", 2))
        entry_rsi_max = float(cfg.get("entry_rsi_max", 10.0))

        results: list[FilterResult] = []

        close = float(ohlcv["close"].iloc[-1])
        sma_series = ohlcv["close"].rolling(trend_ma).mean()
        sma_val = float(sma_series.iloc[-1]) if len(ohlcv) >= trend_ma else float("nan")
        rsi_val = float(compute_rsi(ohlcv, period=rsi_period)["rsi"])
        indicators = {
            "rsi2": round(rsi_val, 2),
            "sma200": round(sma_val, 2) if not math.isnan(sma_val) else None,
            "close": round(close, 2),
        }

        # PositionGate — in-position exits are handled in emit_signal_card
        results.append(FilterResult(
            passed=True,
            filter_name="PositionGate",
            reason="in position" if rsi2_state.in_position else "flat — entry rules apply",
            adjustments={"indicators": indicators},
        ))
        if rsi2_state.in_position:
            return results

        # TrendFilter — close must be above the long SMA
        if math.isnan(sma_val):
            results.append(FilterResult(
                passed=False, filter_name="TrendFilter", hard_stop=True,
                reason=f"insufficient_history: {len(ohlcv)} bars < {trend_ma} needed",
            ))
            return results
        if close <= sma_val:
            results.append(FilterResult(
                passed=False, filter_name="TrendFilter", hard_stop=True,
                reason=f"below_200dma: close {close:.2f} <= SMA({trend_ma}) {sma_val:.2f}",
            ))
            return results
        results.append(FilterResult(
            passed=True, filter_name="TrendFilter",
            reason=f"close {close:.2f} > SMA({trend_ma}) {sma_val:.2f}",
        ))

        # RSIEntryGate — RSI(2) must be oversold
        if rsi_val >= entry_rsi_max:
            results.append(FilterResult(
                passed=False, filter_name="RSIEntryGate", hard_stop=True,
                reason=f"rsi_not_oversold: RSI({rsi_period}) {rsi_val:.1f} >= {entry_rsi_max:.0f}",
            ))
            return results
        results.append(FilterResult(
            passed=True, filter_name="RSIEntryGate",
            reason=f"RSI({rsi_period}) {rsi_val:.1f} < {entry_rsi_max:.0f}",
        ))

        # PositionSizer
        # TODO(Phase 10): source account equity from adapter.get_account() via RiskManager
        equity = float(cfg.get("paper_starting_equity", 25000.0))
        size_pct = float(cfg.get("position_size_pct", 5.0))
        shares = math.floor(size_pct / 100.0 * equity / close)
        if shares < 1:
            results.append(FilterResult(
                passed=False, filter_name="PositionSizer", hard_stop=True,
                reason=f"position_size_zero: {size_pct}% of ${equity:.0f} < 1 share at ${close:.2f}",
            ))
            return results
        results.append(FilterResult(
            passed=True, filter_name="PositionSizer",
            reason=f"{shares} shares (~${shares * close:.0f})",
            adjustments={"shares": shares},
        ))
        return results

    def simulate_trade(
        self,
        card: SignalCard,
        future_ohlcv: pd.DataFrame,
        future_options_chains=None,
    ) -> TradeResult:
        from src.strategies.momentum.backtester import simulate_rsi2_trade

        if not card.legs:
            entry = card.signal_timestamp.date()
            return TradeResult(
                entry_date=entry, exit_date=entry, signal_type=card.signal_type,
                ticker=card.ticker, pnl=0.0, metadata={"reason": "no equity leg"},
            )

        leg = card.legs[0]
        return simulate_rsi2_trade(
            ticker=card.ticker,
            entry_date=card.signal_timestamp.date(),
            entry_price=leg.limit_price or card.underlying_price,
            shares=leg.qty,
            future_ohlcv=future_ohlcv,
            exit_rsi_min=float(self._cfg.get("exit_rsi_min", 70.0)),
            max_hold_days=int(self._cfg.get("max_hold_days", 5)),
            rsi_period=int(self._cfg.get("rsi_period", 2)),
            commission_per_share=float(self._cfg.get("commission_per_share", 0.0)),
            slippage_pct=float(self._cfg.get("slippage_pct", 0.05)),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _exit_or_hold_card(
        self,
        ticker: str,
        state: RSI2State,
        indicators: dict[str, Any],
        close: float,
    ) -> SignalCard:
        exit_rsi_min = float(self._cfg.get("exit_rsi_min", 70.0))
        max_hold_days = int(self._cfg.get("max_hold_days", 5))
        rsi_val = indicators.get("rsi2", 0.0)

        days_held = 0
        if state.entry_date:
            days_held = (date.today() - date.fromisoformat(state.entry_date)).days
        payload = {"days_held": days_held, "entry_price": state.entry_price}

        exit_reason = None
        if rsi_val > exit_rsi_min:
            exit_reason = f"RSI(2) {rsi_val:.1f} > exit threshold {exit_rsi_min:.0f}"
        elif days_held >= max_hold_days:
            exit_reason = f"held {days_held}d >= max_hold_days {max_hold_days}"

        if exit_reason is None:
            return SignalCard(
                strategy_name=self.name,
                ticker=ticker,
                signal_type="NO_SIGNAL",
                underlying_price=close,
                indicators=indicators,
                payload=payload,
                no_signal_reason=f"holding_position: {days_held}d held, RSI(2) {rsi_val:.1f}",
            )

        logger.info("signal_emitted", ticker=ticker, signal_type="SELL_EQUITY",
                    shares=state.shares, reason=exit_reason)
        return SignalCard(
            strategy_name=self.name,
            ticker=ticker,
            signal_type="SELL_EQUITY",
            underlying_price=close,
            legs=[Leg(
                asset_class="equity",
                symbol=ticker,
                side="sell",
                qty=state.shares,
                order_type="limit",
                limit_price=round(close, 2),
            )],
            indicators=indicators,
            payload=payload,
            confidence_tier="HIGH",
            confidence_rationale=exit_reason,
        )

    @staticmethod
    def _collect_adjustments(results: list[FilterResult]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for r in results:
            merged.update(r.adjustments)
        return merged
