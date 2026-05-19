"""
PutCreditSpreadStrategy: bull put spread options income strategy.

Sells a put at ~0.30 delta and buys a put at ~0.15 delta at the same expiry,
collecting a net credit. Defined-risk alternative to the naked cash-secured put
(Wheel Phase A) — capital-efficient for ETFs and high-priced underlyings.

Filter chain (6 filters, cheapest-first):
  SpreadOpenGate → EMA → RSI → IVRankGate → OptionsChain → SpreadLegBuilder
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
from src.indicator_engine import compute_iv_rank
from src.iv_history import append_iv_sample, load_iv_series
from src.signal_output import Leg, SignalCard, compute_confidence_tier
from src.strategies.base import FilterResult, TradeResult
from src.strategies.registry import register
from src.strategies.spreads.filters import (
    IVRankGateFilter,
    SpreadLegBuilderFilter,
    SpreadOpenGateFilter,
)
from src.strategies.spreads.state import PCSState
from src.strategies.wheel.filters import (
    EMATrendFilter,
    OptionsChainFilter,
    RSIFilter,
)

if TYPE_CHECKING:
    from src.adapters.base import DataAdapter

logger = structlog.get_logger(__name__)


@register("put_credit_spread")
class PutCreditSpreadStrategy:
    name = "put_credit_spread"

    def __init__(self):
        self._cfg: dict[str, Any] = {}
        self._filters: list = []

    # ------------------------------------------------------------------
    # Strategy Protocol
    # ------------------------------------------------------------------

    def load_config(self, config: dict[str, Any]) -> None:
        self._cfg = config
        ema = EMATrendFilter(ema_period=config.get("ema_period", 50))
        rsi = RSIFilter(
            rsi_min=config.get("rsi_min", 35.0),
            rsi_max=config.get("rsi_max", 65.0),
        )
        iv_gate = IVRankGateFilter(min_iv_rank=config.get("min_iv_rank", 25.0))
        options = OptionsChainFilter()
        leg_builder = SpreadLegBuilderFilter(
            short_delta_target=config.get("short_delta_target", 0.30),
            short_delta_tolerance=config.get("short_delta_tolerance", 0.05),
            long_delta_target=config.get("long_delta_target", 0.15),
            long_delta_tolerance=config.get("long_delta_tolerance", 0.07),
            min_credit=config.get("min_credit", 0.40),
            max_spread_width=config.get("max_spread_width", 15.0),
        )
        self._filters = [
            SpreadOpenGateFilter(),
            ema,
            rsi,
            iv_gate,
            options,
            leg_builder,
        ]

    def get_state_schema(self) -> type[BaseModel]:
        return PCSState

    def emit_signal_card(
        self,
        ticker: str,
        adapter: "DataAdapter",
        state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> SignalCard:
        if not self._filters:
            raise RuntimeError("Call load_config() before emit_signal_card()")

        iv_history_dir = Path((context or {}).get("iv_history_dir", "iv_history"))
        dte_min: int = self._cfg.get("dte_min", 30)
        dte_max: int = self._cfg.get("dte_max", 45)

        ohlcv = adapter.get_ohlcv(ticker, bars=60)
        options_chain = adapter.get_options_chain(ticker, dte_min=dte_min, dte_max=dte_max)
        underlying_price = float(ohlcv["close"].iloc[-1])

        atm_iv = self._get_atm_iv(options_chain, underlying_price)
        iv_series = load_iv_series(ticker, iv_history_dir)
        if atm_iv is not None:
            append_iv_sample(ticker, atm_iv, iv_history_dir)
            iv_series = iv_series + [atm_iv]
        iv_rank = compute_iv_rank(atm_iv, iv_series) if atm_iv is not None else None

        filter_results = self.evaluate(ticker, ohlcv, options_chain, state, iv_rank=iv_rank)
        hard_stopped = any(not r.passed and r.hard_stop for r in filter_results)

        if hard_stopped:
            stop = next(r for r in filter_results if not r.passed and r.hard_stop)
            logger.info("signal_blocked", ticker=ticker, filter=stop.filter_name, reason=stop.reason)
            return SignalCard(
                strategy_name=self.name,
                ticker=ticker,
                signal_type="NO_SIGNAL",
                underlying_price=underlying_price,
                no_signal_reason=f"{stop.filter_name}: {stop.reason}",
            )

        adj = self._collect_adjustments(filter_results)
        soft_flags = self._collect_soft_flags(filter_results)

        short_strike: float = adj["short_strike"]
        long_strike: float = adj["long_strike"]
        net_credit: float = adj["net_credit"]
        spread_width: float = adj["spread_width"]
        rec_expiry: str = adj.get("recommended_expiry", "")
        dte: int | None = adj.get("dte")
        max_profit: float = adj.get("max_profit", net_credit * 100)
        max_loss: float = adj.get("max_loss", (spread_width - net_credit) * 100)
        short_delta: float = adj.get("short_delta", adj.get("delta_estimate", 0.0))
        long_delta: float = adj.get("long_delta", 0.0)

        delta_distance = abs(abs(short_delta) - 0.30)
        tier, rationale = compute_confidence_tier(soft_flags, delta_distance, iv_rank)

        risk_flags: list[str] = []
        if "iv_rank_missing" in soft_flags:
            risk_flags.append("IV rank unavailable — credit quality uncertain")

        legs: list[Leg] = [
            Leg(
                asset_class="option",
                symbol=ticker,
                side="sell",
                qty=1,
                order_type="limit",
                limit_price=round(net_credit, 2),
                strike=short_strike,
                expiry=rec_expiry,
                option_type="put",
                delta_estimate=short_delta,
            ),
            Leg(
                asset_class="option",
                symbol=ticker,
                side="buy",
                qty=1,
                order_type="limit",
                limit_price=None,
                strike=long_strike,
                expiry=rec_expiry,
                option_type="put",
                delta_estimate=long_delta,
            ),
        ]

        payload: dict[str, Any] = {
            "dte": dte,
            "net_credit": net_credit,
            "spread_width": spread_width,
            "max_profit": max_profit,
            "max_loss": max_loss,
        }
        if iv_rank is not None:
            payload["iv_rank"] = iv_rank

        logger.info("signal_emitted", ticker=ticker, signal_type="SELL_PUT_SPREAD", tier=tier,
                    short_strike=short_strike, long_strike=long_strike, net_credit=net_credit)

        return SignalCard(
            strategy_name=self.name,
            ticker=ticker,
            signal_type="SELL_PUT_SPREAD",
            underlying_price=underlying_price,
            legs=legs,
            indicators={},
            payload=payload,
            confidence_tier=tier,
            confidence_rationale=rationale,
            risk_flags=risk_flags,
        )

    def evaluate(
        self,
        ticker: str,
        ohlcv: pd.DataFrame,
        options_chain: pd.DataFrame,
        state: dict[str, Any],
        iv_rank: float | None = None,
    ) -> list[FilterResult]:
        if not self._filters:
            raise RuntimeError("Call load_config() before evaluate()")

        pcs_state = PCSState.from_dict(state) if state else PCSState.default()
        logger.debug("evaluate_start", ticker=ticker, open_spread=pcs_state.open_spread)

        market_data: dict[str, Any] = {
            "ticker": ticker,
            "ohlcv": ohlcv,
            "options_chain": options_chain,
            "iv_rank": iv_rank,
        }

        results: list[FilterResult] = []
        for f in self._filters:
            r = f.run(market_data, pcs_state, self._cfg)
            results.append(r)
            market_data.update(r.adjustments)
            if not r.passed and r.hard_stop:
                logger.debug("evaluate_hard_stop", ticker=ticker, filter=f.name, reason=r.reason)
                return results

        logger.debug("evaluate_complete", ticker=ticker, filters_run=len(results))
        return results

    def simulate_trade(
        self,
        card: SignalCard,
        future_ohlcv: pd.DataFrame,
        future_options_chains=None,
    ) -> TradeResult:
        from src.strategies.spreads.backtester import simulate_pcs_trade

        if len(card.legs) < 2:
            from datetime import date as _date
            return TradeResult(
                entry_date=card.signal_timestamp.date(),
                exit_date=card.signal_timestamp.date(),
                signal_type=card.signal_type,
                ticker=card.ticker,
                pnl=0.0,
                metadata={"reason": "spread requires 2 legs"},
            )

        short_leg = card.legs[0]
        long_leg = card.legs[1]
        entry_date = card.signal_timestamp.date()
        dte_target = int(card.payload.get("dte", self._cfg.get("dte_min", 30) + 7))
        net_credit = float(card.payload.get("net_credit", 0.0))

        entry_ts = pd.Timestamp(entry_date, tz="UTC")
        entry_ohlcv = future_ohlcv[future_ohlcv.index <= entry_ts]
        if entry_ohlcv.empty:
            entry_ohlcv = future_ohlcv.iloc[:1]

        return simulate_pcs_trade(
            ticker=card.ticker,
            short_strike=short_leg.strike or card.underlying_price * 0.93,
            long_strike=long_leg.strike or card.underlying_price * 0.88,
            entry_date=entry_date,
            dte_target=dte_target,
            net_credit=net_credit,
            entry_ohlcv=entry_ohlcv,
            future_ohlcv=future_ohlcv,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_atm_iv(options_chain: pd.DataFrame, underlying_price: float) -> float | None:
        if options_chain.empty or "iv" not in options_chain.columns:
            return None
        puts = options_chain[options_chain["option_type"] == "put"].copy()
        if puts.empty:
            return None
        puts["_strike_dist"] = (puts["strike"] - underlying_price).abs()
        atm = puts.loc[puts["_strike_dist"].idxmin()]
        iv = atm.get("iv")
        return float(iv) if pd.notna(iv) else None

    @staticmethod
    def _collect_adjustments(results: list[FilterResult]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for r in results:
            merged.update(r.adjustments)
        return merged

    @staticmethod
    def _collect_soft_flags(results: list[FilterResult]) -> list[str]:
        return [r.adjustments["soft_flag"] for r in results if r.adjustments.get("soft_flag")]
