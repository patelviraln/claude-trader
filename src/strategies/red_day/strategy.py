"""
RedDayCSPStrategy: sell cash-secured puts on red days where the dip is
sentiment/market drag, not a fundamental break.

Ported from the red-day-sentiment-scanner skill. Two-pass filter chain:

Pass 1 (no chain/LLM I/O): Open Position Gate → Dip Trigger → Residual
                            → Earnings Gate
Pass 2 (chain + LLM):       Options Chain → IV Behaviour → Attribution
                            → Delta Target (reused from wheel)

On green days pass 1 stops at Dip Trigger, so no options chain is fetched
and no LLM call is made.
"""
from __future__ import annotations

import tomllib
from datetime import date, timedelta
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
from src.strategies.red_day.filters import (
    AttributionFilter,
    DipTriggerFilter,
    EarningsGateFilter,
    IVBehaviorFilter,
    OpenPositionGateFilter,
    ResidualFilter,
)
from src.strategies.red_day.state import RedDayState
from src.strategies.registry import register
from src.strategies.wheel.filters import DeltaTargetFilter, OptionsChainFilter
from src.strategies.wheel.strategy import WheelStrategy

if TYPE_CHECKING:
    from src.adapters.base import DataAdapter

logger = structlog.get_logger(__name__)

OHLCV_BARS = 130  # 120 daily returns for beta + headroom


@register("red_day_csp")
class RedDayCSPStrategy:
    name = "red_day_csp"

    def __init__(self):
        self._cfg: dict[str, Any] = {}
        self._pass1: list = []
        self._pass2: list = []

    # ------------------------------------------------------------------
    # Strategy Protocol methods
    # ------------------------------------------------------------------

    def load_config(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._pass1 = [
            OpenPositionGateFilter(),
            DipTriggerFilter(),
            ResidualFilter(),
            EarningsGateFilter(),
        ]
        self._pass2 = [
            OptionsChainFilter(),
            IVBehaviorFilter(),
            AttributionFilter(),
            DeltaTargetFilter(
                delta_target=config.get("delta_target", 0.22),
                delta_tolerance=config.get("delta_tolerance", 0.03),
            ),
        ]

    def get_state_schema(self) -> type[BaseModel]:
        return RedDayState

    def emit_signal_card(
        self,
        ticker: str,
        adapter: "DataAdapter",
        state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> SignalCard:
        if not self._pass1:
            raise RuntimeError("Call load_config() before emit_signal_card()")

        context = context or {}
        iv_history_dir = Path(context.get("iv_history_dir", "iv_history"))
        rd_state = RedDayState.from_dict(state) if state else RedDayState.default()

        ohlcv = adapter.get_ohlcv(ticker, bars=OHLCV_BARS)
        underlying_price = float(ohlcv["close"].iloc[-1])

        market_data: dict[str, Any] = {
            "ticker": ticker,
            "ohlcv": ohlcv,
            "today": context.get("today") or date.today(),
        }
        # Pre-fetched values may be injected by batch runs and tests
        for key in ("earnings_date", "attribution"):
            if key in context:
                market_data[key] = context[key]
        self._attach_benchmarks(ticker, adapter, market_data, context)

        results = self._run_filters(self._pass1, market_data, rd_state)
        stop = self._hard_stop(results)
        if stop:
            return self._no_signal(ticker, underlying_price, stop)

        # Pass 1 clear — fetch the chain (capped by the earnings gate) and IV
        dte_min = int(self._cfg.get("dte_min", 30))
        dte_max = int(market_data.get("dte_max_effective",
                                      self._cfg.get("dte_max", 45)))
        options_chain = adapter.get_options_chain(ticker, dte_min=dte_min,
                                                  dte_max=dte_max)
        market_data["options_chain"] = options_chain

        atm_iv = WheelStrategy._get_atm_iv(options_chain, underlying_price)
        iv_series = load_iv_series(ticker, iv_history_dir)
        if atm_iv is not None:
            append_iv_sample(ticker, atm_iv, iv_history_dir)
            iv_series = iv_series + [atm_iv]
        market_data["atm_iv"] = atm_iv
        market_data["iv_series"] = iv_series
        iv_rank = compute_iv_rank(atm_iv, iv_series) if atm_iv is not None else None

        results += self._run_filters(self._pass2, market_data, rd_state)
        stop = self._hard_stop(results)
        if stop:
            return self._no_signal(ticker, underlying_price, stop)

        adj: dict[str, Any] = {}
        for r in results:
            adj.update(r.adjustments)
        soft_flags = [r.adjustments["soft_flag"] for r in results
                      if r.adjustments.get("soft_flag")]

        delta_estimate: float | None = adj.get("delta_estimate")
        if delta_estimate is None:
            raise RuntimeError(
                "delta_estimate missing from combined filter adjustments; "
                "verify DeltaTargetFilter ran and passed before signal assembly."
            )

        rec_strike: float | None = adj.get("recommended_strike")
        rec_expiry: str | None = adj.get("recommended_expiry")
        dte: int | None = adj.get("dte")
        option_mid = WheelStrategy._get_contract_mid(
            options_chain, rec_strike, rec_expiry, "put"
        )

        delta_target = self._cfg.get("delta_target", 0.22)
        tier, rationale = compute_confidence_tier(
            soft_flags, abs(abs(delta_estimate) - delta_target), iv_rank
        )

        indicators: dict[str, Any] = {
            "day_move_pct": adj.get("day_move_pct"),
            "residual_pct": adj.get("residual_pct"),
        }
        indicators = {k: v for k, v in indicators.items() if v is not None}

        risk_flags = []
        if "iv_not_expanding" in soft_flags:
            risk_flags.append("IV flat/falling on the dip — orderly repricing, weaker premium")
        if "attribution_unavailable" in soft_flags:
            risk_flags.append("Attribution unavailable — allowed on SYMPATHY residual only")
        if adj.get("residual_tag") == "MIXED":
            risk_flags.append("Dip partially idiosyncratic (MIXED residual)")

        legs: list[Leg] = []
        if rec_strike is not None and rec_expiry is not None:
            legs = [Leg(
                asset_class="option",
                symbol=ticker,
                side="sell",
                qty=1,
                order_type="limit",
                limit_price=option_mid,
                strike=rec_strike,
                expiry=rec_expiry,
                option_type="put",
                delta_estimate=delta_estimate,
            )]

        payload: dict[str, Any] = {
            "dte": dte,
            "residual_tag": adj.get("residual_tag"),
            "attribution_category": adj.get("attribution_category"),
            "attribution_reason": adj.get("attribution_reason"),
            "day_move_pct": adj.get("day_move_pct"),
        }
        if iv_rank is not None:
            payload["iv_rank"] = iv_rank

        logger.info("signal_emitted", ticker=ticker, signal_type="SELL_PUT",
                    strategy=self.name, tier=tier,
                    residual_tag=adj.get("residual_tag"),
                    attribution=adj.get("attribution_category"))

        return SignalCard(
            strategy_name=self.name,
            ticker=ticker,
            signal_type="SELL_PUT",
            underlying_price=underlying_price,
            legs=legs,
            indicators=indicators,
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
        extra: dict[str, Any] | None = None,
    ) -> list[FilterResult]:
        """Run the full chain against provided data (test/backtest entry point).

        extra may inject: spy_ohlcv, sector_ohlcv, today, earnings_date,
        attribution, atm_iv, iv_series.
        """
        if not self._pass1:
            raise RuntimeError("Call load_config() before evaluate()")

        rd_state = RedDayState.from_dict(state) if state else RedDayState.default()
        market_data: dict[str, Any] = {
            "ticker": ticker,
            "ohlcv": ohlcv,
            "options_chain": options_chain,
            "today": date.today(),
        }
        if extra:
            market_data.update(extra)

        results = self._run_filters(self._pass1, market_data, rd_state)
        if self._hard_stop(results):
            return results
        self._cap_chain(market_data)
        return results + self._run_filters(self._pass2, market_data, rd_state)

    def simulate_trade(
        self,
        card: SignalCard,
        future_ohlcv: pd.DataFrame,
        future_options_chains=None,
    ) -> TradeResult:
        """Same P&L model as a wheel short put (BS pricing)."""
        from src.strategies.wheel.backtester import simulate_wheel_trade

        if not card.legs:
            d = card.signal_timestamp.date()
            return TradeResult(entry_date=d, exit_date=d,
                               signal_type=card.signal_type, ticker=card.ticker,
                               pnl=0.0, metadata={"reason": "no legs in card"})

        leg = card.legs[0]
        entry_date = card.signal_timestamp.date()
        dte_target = int(card.payload.get("dte") or self._cfg.get("dte_min", 30))
        entry_ts = pd.Timestamp(entry_date, tz="UTC")
        entry_ohlcv = future_ohlcv[future_ohlcv.index <= entry_ts]
        if entry_ohlcv.empty:
            entry_ohlcv = future_ohlcv.iloc[:1]

        return simulate_wheel_trade(
            signal_type="SELL_PUT",
            ticker=card.ticker,
            strike=leg.strike or card.underlying_price * 0.95,
            entry_date=entry_date,
            dte_target=dte_target,
            entry_ohlcv=entry_ohlcv,
            future_ohlcv=future_ohlcv,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _attach_benchmarks(
        self,
        ticker: str,
        adapter: "DataAdapter",
        market_data: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Fetch SPY + sector ETF bars, memoized across a batch via context."""
        cache: dict[str, pd.DataFrame | None] = context.setdefault("benchmark_ohlcv", {})

        def fetch(symbol: str) -> pd.DataFrame | None:
            if symbol not in cache:
                try:
                    cache[symbol] = adapter.get_ohlcv(symbol, bars=OHLCV_BARS)
                except Exception as exc:
                    logger.warning("red_day.benchmark_fetch_failed",
                                   symbol=symbol, error=str(exc))
                    cache[symbol] = None
            return cache[symbol]

        spy = fetch("SPY")
        if spy is not None:
            market_data["spy_ohlcv"] = spy

        sector_symbol = self._cfg.get("sector_etfs", {}).get(ticker)
        if sector_symbol:
            sector = fetch(sector_symbol)
            if sector is not None:
                market_data["sector_ohlcv"] = sector

    def _run_filters(self, filters: list, market_data: dict, state: BaseModel) -> list[FilterResult]:
        results: list[FilterResult] = []
        for f in filters:
            r = f.run(market_data, state, self._cfg)
            results.append(r)
            market_data.update(r.adjustments)
            if not r.passed and r.hard_stop:
                break
        return results

    def _cap_chain(self, market_data: dict[str, Any]) -> None:
        """Drop chain rows past the earnings-capped expiry (evaluate() path,
        where the chain was provided rather than fetched with the cap)."""
        cap = market_data.get("dte_max_effective")
        chain: pd.DataFrame = market_data.get("options_chain")
        if cap is None or chain is None or chain.empty or "expiry" not in chain.columns:
            return
        cutoff = market_data.get("today", date.today()) + timedelta(days=int(cap))
        expiries = pd.to_datetime(chain["expiry"]).dt.date
        market_data["options_chain"] = chain[expiries <= cutoff].reset_index(drop=True)

    @staticmethod
    def _hard_stop(results: list[FilterResult]) -> FilterResult | None:
        for r in results:
            if not r.passed and r.hard_stop:
                return r
        return None

    def _no_signal(self, ticker: str, underlying_price: float,
                   stop: FilterResult) -> SignalCard:
        logger.info("signal_blocked", ticker=ticker, strategy=self.name,
                    filter=stop.filter_name, reason=stop.reason)
        return SignalCard(
            strategy_name=self.name,
            ticker=ticker,
            signal_type="NO_SIGNAL",
            underlying_price=underlying_price,
            no_signal_reason=f"{stop.filter_name}: {stop.reason}",
        )


# ------------------------------------------------------------------
# Config loader helper
# ------------------------------------------------------------------

def load_red_day_config(config_path: str | Path = "config/red_day_csp.toml") -> dict[str, Any]:
    path = Path(config_path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return raw.get("red_day_csp", raw)
