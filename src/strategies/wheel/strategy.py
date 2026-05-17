"""
WheelStrategy: two-pass filter chain implementing the Wheel options strategy.

Pass 1 (cheapest first, no I/O): Phase Gate → EMA → RSI → BB
Pass 2 (requires I/O):           Options Chain → Delta → Volume Profile
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
from src.indicator_engine import compute_iv_rank
from src.iv_history import append_iv_sample, load_iv_series
from src.signal_output import Leg, SignalCard, compute_confidence_tier
from src.strategies.base import FilterResult
from src.strategies.registry import register
from src.strategies.wheel.filters import (
    BollingerFilter,
    DeltaTargetFilter,
    EMATrendFilter,
    OptionsChainFilter,
    PhaseGateFilter,
    RSIFilter,
    VolumeProfileFilter,
)
from src.strategies.wheel.state import WheelState

if TYPE_CHECKING:
    from src.adapters.base import DataAdapter

from src.strategies.base import TradeResult

logger = structlog.get_logger(__name__)


@register("wheel")
class WheelStrategy:
    name = "wheel"

    def __init__(self):
        self._cfg: dict[str, Any] = {}
        self._phase_gate = PhaseGateFilter()
        self._ema: EMATrendFilter | None = None
        self._rsi: RSIFilter | None = None
        self._bb: BollingerFilter | None = None
        self._options: OptionsChainFilter = OptionsChainFilter()
        self._delta: DeltaTargetFilter | None = None
        self._volume: VolumeProfileFilter | None = None
        self._filters: list = []

    # ------------------------------------------------------------------
    # Strategy Protocol methods
    # ------------------------------------------------------------------

    def load_config(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._ema = EMATrendFilter(ema_period=config.get("ema_period", 50))
        self._rsi = RSIFilter(
            rsi_min=config.get("rsi_min", 35.0),
            rsi_max=config.get("rsi_max", 65.0),
        )
        self._bb = BollingerFilter(
            period=config.get("bb_period", 20),
            std_dev=config.get("bb_std_dev", 2.0),
        )
        self._delta = DeltaTargetFilter(
            delta_target=config.get("delta_target", 0.30),
            delta_tolerance=config.get("delta_tolerance", 0.05),
        )
        self._volume = VolumeProfileFilter(
            lookback=config.get("volume_lookback_bars", 20),
        )
        self._filters = [
            self._phase_gate,
            self._ema,
            self._rsi,
            self._bb,
            self._options,
            self._delta,
            self._volume,
        ]

    def get_state_schema(self) -> type[BaseModel]:
        return WheelState

    def emit_signal_card(
        self,
        ticker: str,
        adapter: "DataAdapter",
        state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> SignalCard:
        """Fetch market data, run the filter chain, and return a SignalCard."""
        if not self._filters:
            raise RuntimeError("Call load_config() before emit_signal_card()")

        iv_history_dir = Path((context or {}).get("iv_history_dir", "iv_history"))
        dte_min: int = self._cfg.get("dte_min", 30)
        dte_max: int = self._cfg.get("dte_max", 45)

        ohlcv = adapter.get_ohlcv(ticker, bars=60)
        options_chain = adapter.get_options_chain(ticker, dte_min=dte_min, dte_max=dte_max)
        underlying_price = float(ohlcv["close"].iloc[-1])

        # IV rank: persist ATM IV then compute rank from rolling history
        atm_iv = self._get_atm_iv(options_chain, underlying_price)
        iv_series = load_iv_series(ticker, iv_history_dir)
        if atm_iv is not None:
            append_iv_sample(ticker, atm_iv, iv_history_dir)
            iv_series = iv_series + [atm_iv]
        iv_rank = compute_iv_rank(atm_iv, iv_series) if atm_iv is not None else None

        filter_results = self.evaluate(ticker, ohlcv, options_chain, state)
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

        delta_estimate: float | None = adj.get("delta_estimate")
        if delta_estimate is None:
            raise RuntimeError(
                "delta_estimate missing from combined filter adjustments; "
                "verify DeltaTargetFilter ran and passed before signal assembly."
            )

        phase = state.get("phase", "A")
        signal_type = "SELL_PUT" if phase == "A" else "SELL_CALL"
        option_type = "put" if phase == "A" else "call"

        rec_strike: float | None = adj.get("recommended_strike")
        rec_expiry: str | None = adj.get("recommended_expiry")
        dte: int | None = adj.get("dte")
        option_mid = self._get_contract_mid(options_chain, rec_strike, rec_expiry, option_type)

        delta_distance = abs(delta_estimate) - 0.30
        tier, rationale = compute_confidence_tier(soft_flags, abs(delta_distance), iv_rank)

        indicators: dict[str, Any] = {}
        for key in ("ema50", "ema50_slope", "rsi14", "bb_percent_b",
                    "volume_node_nearest", "volume_node_type"):
            if key in adj:
                indicators[key] = adj[key]

        risk_flags = []
        if "bb_unfavorable" in soft_flags:
            risk_flags.append("BB position unfavorable for entry")
        if "volume_node_divergence" in soft_flags:
            pct = adj.get("hvn_divergence_pct", 0)
            risk_flags.append(f"Strike diverges {pct:.1f}% from volume node")

        legs: list[Leg] = []
        if rec_strike is not None and rec_expiry is not None:
            legs = [
                Leg(
                    asset_class="option",
                    symbol=ticker,
                    side="sell",
                    qty=1,
                    order_type="limit",
                    limit_price=option_mid,
                    strike=rec_strike,
                    expiry=rec_expiry,
                    option_type=option_type,
                    delta_estimate=delta_estimate,
                )
            ]

        payload: dict[str, Any] = {"dte": dte}
        if iv_rank is not None:
            payload["iv_rank"] = iv_rank

        logger.info("signal_emitted", ticker=ticker, signal_type=signal_type, tier=tier)

        return SignalCard(
            strategy_name=self.name,
            ticker=ticker,
            signal_type=signal_type,
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
    ) -> list[FilterResult]:
        """Run the full filter chain and return ordered FilterResult list."""
        if not self._filters:
            raise RuntimeError("Call load_config() before evaluate()")

        wheel_state = WheelState.from_dict(state) if state else WheelState.default()
        logger.debug("evaluate_start", ticker=ticker, phase=wheel_state.phase)

        market_data: dict[str, Any] = {
            "ticker": ticker,
            "ohlcv": ohlcv,
            "options_chain": options_chain,
        }

        results: list[FilterResult] = []
        for f in self._filters:
            r = f.run(market_data, wheel_state, self._cfg)
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
        """Simulate a Wheel trade from its signal card using BS pricing."""
        from src.strategies.wheel.backtester import simulate_wheel_trade

        if not card.legs:
            from datetime import date
            return TradeResult(
                entry_date=card.signal_timestamp.date(),
                exit_date=card.signal_timestamp.date(),
                signal_type=card.signal_type,
                ticker=card.ticker,
                pnl=0.0,
                metadata={"reason": "no legs in card"},
            )

        leg = card.legs[0]
        entry_date = card.signal_timestamp.date()
        dte_target = int(card.payload.get("dte", self._cfg.get("dte_min", 30) + 7))

        # Reconstruct entry OHLCV from future_ohlcv preceding the entry date
        entry_ts = pd.Timestamp(entry_date, tz="UTC")
        entry_ohlcv = future_ohlcv[future_ohlcv.index <= entry_ts]
        if entry_ohlcv.empty:
            entry_ohlcv = future_ohlcv.iloc[:1]

        return simulate_wheel_trade(
            signal_type=card.signal_type,
            ticker=card.ticker,
            strike=leg.strike or card.underlying_price * 0.93,
            entry_date=entry_date,
            dte_target=dte_target,
            entry_ohlcv=entry_ohlcv,
            future_ohlcv=future_ohlcv,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_atm_iv(options_chain: pd.DataFrame, underlying_price: float) -> float | None:
        if options_chain.empty or "iv" not in options_chain.columns:
            return None
        puts = options_chain[options_chain["option_type"] == "put"].copy()
        if puts.empty:
            return None
        puts["strike_dist"] = (puts["strike"] - underlying_price).abs()
        atm = puts.loc[puts["strike_dist"].idxmin()]
        iv = atm.get("iv")
        return float(iv) if pd.notna(iv) else None

    @staticmethod
    def _collect_adjustments(filter_results: list[FilterResult]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for r in filter_results:
            merged.update(r.adjustments)
        return merged

    @staticmethod
    def _collect_soft_flags(filter_results: list[FilterResult]) -> list[str]:
        return [
            r.adjustments["soft_flag"]
            for r in filter_results
            if r.adjustments.get("soft_flag")
        ]

    @staticmethod
    def _get_contract_mid(
        options_chain: pd.DataFrame,
        strike: float | None,
        expiry: str | None,
        option_type: str,
    ) -> float | None:
        if options_chain.empty or strike is None or expiry is None:
            return None
        try:
            expiry_date = pd.Timestamp(expiry).date()
        except Exception:
            return None
        mask = (
            (options_chain["strike"] == strike)
            & (options_chain["expiry"].apply(
                lambda d: d if isinstance(d, type(expiry_date)) else pd.Timestamp(d).date()
            ) == expiry_date)
            & (options_chain["option_type"] == option_type)
        )
        matches = options_chain[mask]
        if matches.empty:
            return None
        row = matches.iloc[0]
        bid = float(row.get("bid") or 0.0)
        ask = float(row.get("ask") or 0.0)
        mid = (bid + ask) / 2
        return round(mid, 4) if mid > 0 else None


# ------------------------------------------------------------------
# Config loader helper
# ------------------------------------------------------------------

def load_wheel_config(config_path: str | Path = "config/wheel.toml") -> dict[str, Any]:
    path = Path(config_path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return raw.get("wheel", raw)
