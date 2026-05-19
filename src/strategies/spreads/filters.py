"""
Put Credit Spread strategy filter classes.

Chain order: SpreadOpenGate → EMA → RSI → IVRankGate → OptionsChain → SpreadLegBuilder

Reuses EMATrendFilter, RSIFilter, and OptionsChainFilter from wheel.filters verbatim —
they are pure market-data filters with no Wheel-specific state assumptions.
"""
from __future__ import annotations

import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
import pandas as pd
from src.strategies.base import FilterResult
from src.strategies.spreads.state import PCSState

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Filter 1 — Spread Open Gate (HARD STOP if spread already open)
# ---------------------------------------------------------------------------

class SpreadOpenGateFilter:
    name = "spread_open_gate"

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        pcs: PCSState = state  # type: ignore[assignment]
        if pcs.open_spread:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=(
                    f"Spread already open: short ${pcs.short_strike} / long ${pcs.long_strike}, "
                    f"expiry {pcs.expiry}"
                ),
                hard_stop=True,
            )
        else:
            r = FilterResult(
                passed=True,
                filter_name=self.name,
                reason="No open spread — proceed",
            )
        logger.debug("filter_result", filter=self.name, passed=r.passed, hard_stop=r.hard_stop)
        return r


# ---------------------------------------------------------------------------
# Filter 4 — IV Rank Gate (HARD STOP if IV rank too low)
# Reads iv_rank from market_data (pre-populated by emit_signal_card).
# ---------------------------------------------------------------------------

class IVRankGateFilter:
    name = "iv_rank_gate"

    def __init__(self, min_iv_rank: float = 25.0):
        self.min_iv_rank = min_iv_rank

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        iv_rank = market_data.get("iv_rank")

        if iv_rank is None:
            # IV history insufficient — soft flag, don't hard-stop (adapter may not have history yet)
            r = FilterResult(
                passed=True,
                filter_name=self.name,
                reason="IV rank unavailable — proceeding cautiously",
                adjustments={"soft_flag": "iv_rank_missing"},
            )
        elif iv_rank < self.min_iv_rank:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=(
                    f"IV rank {iv_rank:.1f} below minimum {self.min_iv_rank:.1f} — "
                    f"spread premiums too thin"
                ),
                hard_stop=True,
            )
        else:
            r = FilterResult(
                passed=True,
                filter_name=self.name,
                reason=f"IV rank {iv_rank:.1f} >= {self.min_iv_rank:.1f} — adequate premium environment",
                adjustments={"iv_rank": iv_rank},
            )
        logger.debug("filter_result", filter=self.name, passed=r.passed, iv_rank=iv_rank)
        return r


# ---------------------------------------------------------------------------
# Filter 6 — Spread Leg Builder (HARD STOP if no valid pair found)
# Finds short put (~short_delta) and long put (~long_delta) at same expiry,
# validates credit and width, and emits both strikes into adjustments.
# ---------------------------------------------------------------------------

class SpreadLegBuilderFilter:
    name = "spread_leg_builder"

    def __init__(
        self,
        short_delta_target: float = 0.30,
        short_delta_tolerance: float = 0.05,
        long_delta_target: float = 0.15,
        long_delta_tolerance: float = 0.07,
        min_credit: float = 0.40,
        max_spread_width: float = 15.0,
    ):
        self.short_delta_target = short_delta_target
        self.short_delta_tolerance = short_delta_tolerance
        self.long_delta_target = long_delta_target
        self.long_delta_tolerance = long_delta_tolerance
        self.min_credit = min_credit
        self.max_spread_width = max_spread_width

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        chain: pd.DataFrame = market_data["options_chain"]
        if chain.empty or "option_type" not in chain.columns:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason="No options data available",
                hard_stop=True,
            )
            logger.debug("filter_result", filter=self.name, passed=False, reason="no_chain")
            return r

        puts = chain[chain["option_type"] == "put"].copy()

        if puts.empty:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason="No put options in chain",
                hard_stop=True,
            )
            logger.debug("filter_result", filter=self.name, passed=False)
            return r

        # Short leg — closest to -short_delta_target
        puts["_short_dist"] = (puts["delta"] - (-self.short_delta_target)).abs()
        best_short = puts.loc[puts["_short_dist"].idxmin()]
        if float(best_short["_short_dist"]) > self.short_delta_tolerance:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=(
                    f"No short put within delta ±{self.short_delta_tolerance}. "
                    f"Closest: delta={float(best_short['delta']):.3f}"
                ),
                hard_stop=True,
            )
            logger.debug("filter_result", filter=self.name, passed=False, reason="short_leg_miss")
            return r

        short_strike = float(best_short["strike"])
        short_expiry = best_short["expiry"]
        short_dte = int(best_short["dte"]) if "dte" in best_short.index else None
        short_mid = self._mid(best_short)

        # Long leg — same expiry, lower strike, closest to -long_delta_target
        same_expiry = puts[puts["expiry"] == short_expiry].copy()
        long_candidates = same_expiry[same_expiry["strike"] < short_strike].copy()

        if long_candidates.empty:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=f"No lower-strike put found at expiry {short_expiry} for long leg",
                hard_stop=True,
            )
            logger.debug("filter_result", filter=self.name, passed=False, reason="long_leg_no_candidates")
            return r

        long_candidates["_long_dist"] = (long_candidates["delta"] - (-self.long_delta_target)).abs()
        best_long = long_candidates.loc[long_candidates["_long_dist"].idxmin()]
        if float(best_long["_long_dist"]) > self.long_delta_tolerance:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=(
                    f"No long put within delta ±{self.long_delta_tolerance}. "
                    f"Closest: delta={float(best_long['delta']):.3f}"
                ),
                hard_stop=True,
            )
            logger.debug("filter_result", filter=self.name, passed=False, reason="long_leg_miss")
            return r

        long_strike = float(best_long["strike"])
        long_mid = self._mid(best_long)

        spread_width = round(short_strike - long_strike, 2)
        net_credit = round(short_mid - long_mid, 4)

        if spread_width > self.max_spread_width:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=f"Spread width ${spread_width:.2f} exceeds max ${self.max_spread_width:.2f}",
                hard_stop=True,
            )
            logger.debug("filter_result", filter=self.name, passed=False, reason="width_exceeded")
            return r

        if net_credit < self.min_credit:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=f"Net credit ${net_credit:.2f} below minimum ${self.min_credit:.2f}",
                hard_stop=True,
            )
            logger.debug("filter_result", filter=self.name, passed=False, reason="credit_too_low")
            return r

        max_profit = round(net_credit * 100, 2)
        max_loss = round((spread_width - net_credit) * 100, 2)

        r = FilterResult(
            passed=True,
            filter_name=self.name,
            reason=(
                f"Sell ${short_strike:.2f}P / Buy ${long_strike:.2f}P @ {short_expiry}  "
                f"width=${spread_width:.2f}  credit=${net_credit:.2f}  "
                f"max_profit=${max_profit:.2f}  max_loss=${max_loss:.2f}"
            ),
            adjustments={
                "short_strike": short_strike,
                "long_strike": long_strike,
                "recommended_expiry": str(short_expiry),
                "dte": short_dte,
                "net_credit": net_credit,
                "spread_width": spread_width,
                "max_profit": max_profit,
                "max_loss": max_loss,
                "short_delta": float(best_short["delta"]),
                "long_delta": float(best_long["delta"]),
                "delta_estimate": float(best_short["delta"]),  # primary delta = short leg
            },
        )
        logger.debug(
            "filter_result", filter=self.name, passed=True,
            short_strike=short_strike, long_strike=long_strike,
            net_credit=net_credit, spread_width=spread_width,
        )
        return r

    @staticmethod
    def _mid(row) -> float:
        bid = float(row.get("bid") or 0.0)
        ask = float(row.get("ask") or 0.0)
        return round((bid + ask) / 2, 4) if (bid + ask) > 0 else 0.0
