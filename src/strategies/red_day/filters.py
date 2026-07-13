"""
Red-day CSP filter chain, ported from the red-day-sentiment-scanner skill:

  Open Position Gate → Dip Trigger → Residual → Attribution → IV Behaviour
  → Earnings Gate → (wheel OptionsChainFilter + DeltaTargetFilter reused)

The core question every filter chain run answers: is this dip a discount
(sentiment/market drag — sell the put) or a warning (fundamental break —
stay away)?
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
from src.indicator_engine import compute_iv_rank
from src.strategies.base import FilterResult

logger = structlog.get_logger(__name__)

RESIDUAL_LOOKBACK_BARS = 120  # daily returns used for the beta estimate


# ---------------------------------------------------------------------------
# Filter 1 — Open Position Gate (duplicate-entry guard)
# ---------------------------------------------------------------------------

class OpenPositionGateFilter:
    name = "open_position_gate"

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        ticker = market_data["ticker"]
        if getattr(state, "open_put_strike", None):
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=(
                    f"Open put already active for {ticker} "
                    f"(strike {state.open_put_strike}, expiry {state.open_put_expiry})"
                ),
                hard_stop=True,
            )
        else:
            r = FilterResult(passed=True, filter_name=self.name,
                             reason="No open position — proceeding")
        logger.debug("filter_result", filter=self.name, passed=r.passed)
        return r


# ---------------------------------------------------------------------------
# Filter 2 — Dip Trigger (per-name threshold)
# ---------------------------------------------------------------------------

class DipTriggerFilter:
    name = "dip_trigger"

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        ticker = market_data["ticker"]
        df: pd.DataFrame = market_data["ohlcv"]
        if len(df) < 2:
            return FilterResult(passed=False, filter_name=self.name,
                                reason="Not enough bars to compute day move",
                                hard_stop=True)

        prev_close = float(df["close"].iloc[-2])
        last_close = float(df["close"].iloc[-1])
        day_move_pct = (last_close / prev_close - 1.0) * 100

        thresholds = config.get("thresholds", {})
        threshold = float(thresholds.get(ticker, thresholds.get("default", -2.0)))

        if day_move_pct > threshold:
            r = FilterResult(
                passed=False,
                filter_name=self.name,
                reason=(
                    f"{ticker} {day_move_pct:+.2f}% today — not through the "
                    f"{threshold:.1f}% dip trigger"
                ),
                hard_stop=True,
                adjustments={"day_move_pct": round(day_move_pct, 2)},
            )
        else:
            r = FilterResult(
                passed=True,
                filter_name=self.name,
                reason=f"{ticker} {day_move_pct:+.2f}% ≤ {threshold:.1f}% trigger — red-day candidate",
                adjustments={"day_move_pct": round(day_move_pct, 2)},
            )
        logger.debug("filter_result", filter=self.name, passed=r.passed,
                     day_move_pct=round(day_move_pct, 2))
        return r


# ---------------------------------------------------------------------------
# Filter 3 — Beta Residual (market/sector drag vs idiosyncratic weakness)
# ---------------------------------------------------------------------------

def _day_move_pct(df: pd.DataFrame) -> float:
    return (float(df["close"].iloc[-1]) / float(df["close"].iloc[-2]) - 1.0) * 100


def compute_beta(stock: pd.DataFrame, benchmark: pd.DataFrame) -> float:
    """Beta from overlapping daily returns; 1.0 when not computable."""
    s = stock["close"].pct_change().dropna().tail(RESIDUAL_LOOKBACK_BARS)
    b = benchmark["close"].pct_change().dropna().tail(RESIDUAL_LOOKBACK_BARS)
    joined = pd.concat([s, b], axis=1, join="inner", keys=["s", "b"]).dropna()
    if len(joined) < 30:
        return 1.0
    var = joined["b"].var()
    if var == 0 or pd.isna(var):
        return 1.0
    return float(joined["s"].cov(joined["b"]) / var)


class ResidualFilter:
    """Tags the dip SYMPATHY / MIXED / IDIOSYNCRATIC. Never a hard stop —
    attribution decides what an idiosyncratic move means."""

    name = "residual"

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        ticker = market_data["ticker"]
        df: pd.DataFrame = market_data["ohlcv"]
        spy: pd.DataFrame | None = market_data.get("spy_ohlcv")
        sector: pd.DataFrame | None = market_data.get("sector_ohlcv")
        day_move = market_data.get("day_move_pct")
        if day_move is None:
            day_move = _day_move_pct(df)

        residual_cfg = config.get("residual", {})
        sympathy_max = float(residual_cfg.get("sympathy_max", 1.0))
        idio_min = float(residual_cfg.get("idiosyncratic_min", 2.5))

        residuals: dict[str, float] = {}
        if spy is not None and len(spy) >= 2:
            beta = compute_beta(df, spy)
            spy_move = _day_move_pct(spy)
            residuals["market"] = day_move - beta * spy_move
        if sector is not None and len(sector) >= 2:
            residuals["sector"] = day_move - _day_move_pct(sector)

        if not residuals:
            # No benchmark data at all — treat as idiosyncratic (conservative)
            r = FilterResult(
                passed=True,
                filter_name=self.name,
                reason="No SPY/sector data — treating move as IDIOSYNCRATIC",
                adjustments={"residual_tag": "IDIOSYNCRATIC",
                             "soft_flag": "residual_unavailable"},
            )
            logger.debug("filter_result", filter=self.name, tag="IDIOSYNCRATIC")
            return r

        # Skill rule: use the MORE FORGIVING residual — if either the market
        # or the sector fully explains the move, it's sympathy.
        best_key = min(residuals, key=lambda k: abs(residuals[k]))
        best = residuals[best_key]

        if abs(best) < sympathy_max:
            tag = "SYMPATHY"
        elif best <= -idio_min:
            tag = "IDIOSYNCRATIC"
        else:
            tag = "MIXED"

        detail = ", ".join(f"{k}={v:+.2f}%" for k, v in residuals.items())
        adjustments: dict[str, Any] = {
            "residual_tag": tag,
            "residual_pct": round(best, 2),
        }
        if tag == "IDIOSYNCRATIC":
            adjustments["soft_flag"] = "idiosyncratic_move"

        r = FilterResult(
            passed=True,
            filter_name=self.name,
            reason=f"Residual {tag} ({detail}; using {best_key} {best:+.2f}%)",
            adjustments=adjustments,
        )
        logger.debug("filter_result", filter=self.name, tag=tag,
                     residual=round(best, 2))
        return r


# ---------------------------------------------------------------------------
# Filter 4 — Attribution (why is it down?)
# ---------------------------------------------------------------------------

class AttributionFilter:
    name = "attribution"

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        ticker = market_data["ticker"]
        residual_tag: str = market_data.get("residual_tag", "IDIOSYNCRATIC")
        day_move: float = market_data.get("day_move_pct", 0.0)

        if not config.get("attribution_enabled", True):
            return FilterResult(
                passed=True, filter_name=self.name,
                reason="Attribution disabled in config",
                adjustments={"attribution_category": "SKIPPED",
                             "soft_flag": "attribution_skipped"},
            )

        # Pre-classified result may be injected for tests / batch runs
        attribution = market_data.get("attribution")
        if attribution is None:
            from src.attribution import classify_dip
            attribution = classify_dip(ticker, day_move, residual_tag)

        category, reason = attribution.category, attribution.reason
        adjustments = {"attribution_category": category,
                       "attribution_reason": reason}

        if category in ("FUNDAMENTAL_BREAK", "EVENT_PENDING"):
            r = FilterResult(
                passed=False, filter_name=self.name,
                reason=f"{category}: {reason}",
                hard_stop=True, adjustments=adjustments,
            )
        elif category == "TRANSIENT":
            r = FilterResult(
                passed=True, filter_name=self.name,
                reason=f"TRANSIENT dip: {reason}",
                adjustments=adjustments,
            )
        elif category == "NO_NEWS":
            # Skill rule: no news + idiosyncratic weakness is a red flag —
            # the tape sometimes knows before the headline.
            if residual_tag == "SYMPATHY":
                r = FilterResult(
                    passed=True, filter_name=self.name,
                    reason="No news, but market/sector explains the move (SYMPATHY)",
                    adjustments=adjustments,
                )
            else:
                r = FilterResult(
                    passed=False, filter_name=self.name,
                    reason=(
                        f"Unexplained {residual_tag.lower()} weakness — no news "
                        "found; recheck in 24h"
                    ),
                    hard_stop=True, adjustments=adjustments,
                )
        else:  # UNAVAILABLE — conservative: only pure sympathy dips may pass
            if residual_tag == "SYMPATHY":
                r = FilterResult(
                    passed=True, filter_name=self.name,
                    reason=f"Attribution unavailable ({reason}) — allowing SYMPATHY dip only",
                    adjustments={**adjustments, "soft_flag": "attribution_unavailable"},
                )
            else:
                r = FilterResult(
                    passed=False, filter_name=self.name,
                    reason=f"Attribution unavailable ({reason}) and move not SYMPATHY — blocked",
                    hard_stop=True, adjustments=adjustments,
                )
        logger.debug("filter_result", filter=self.name, passed=r.passed,
                     category=category)
        return r


# ---------------------------------------------------------------------------
# Filter 5 — IV Behaviour (the premium edge: fear being paid on the dip)
# ---------------------------------------------------------------------------

class IVBehaviorFilter:
    name = "iv_behavior"

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        atm_iv: float | None = market_data.get("atm_iv")
        iv_series: list[float] = market_data.get("iv_series", [])
        ivr_floor = float(config.get("ivr_floor", 30.0))

        if atm_iv is None:
            return FilterResult(
                passed=True, filter_name=self.name,
                reason="No ATM IV available — cannot assess premium richness",
                adjustments={"soft_flag": "iv_unavailable"},
            )

        iv_rank = compute_iv_rank(atm_iv, iv_series)
        if iv_rank is not None and iv_rank < ivr_floor:
            r = FilterResult(
                passed=False, filter_name=self.name,
                reason=f"IV rank {iv_rank:.1f} below floor {ivr_floor:.0f} — premium too thin",
                hard_stop=True,
                adjustments={"iv_rank": iv_rank},
            )
        else:
            adjustments: dict[str, Any] = {}
            if iv_rank is not None:
                adjustments["iv_rank"] = iv_rank
            else:
                adjustments["soft_flag"] = "iv_rank_unavailable"
            # Rising IV on the dip = fear premium being paid = the edge.
            # Flat/falling IV = orderly repricing — tradeable but weaker.
            prev_iv = iv_series[-2] if len(iv_series) >= 2 else None
            if prev_iv is not None and atm_iv <= prev_iv:
                adjustments["soft_flag"] = "iv_not_expanding"
                reason = (
                    f"IV rank {iv_rank if iv_rank is not None else '?'} ok but IV "
                    f"not expanding on the dip ({prev_iv:.3f} → {atm_iv:.3f})"
                )
            else:
                reason = f"IV rank {'%.1f' % iv_rank if iv_rank is not None else 'n/a'} — premium acceptable"
            r = FilterResult(passed=True, filter_name=self.name,
                             reason=reason, adjustments=adjustments)
        logger.debug("filter_result", filter=self.name, passed=r.passed)
        return r


# ---------------------------------------------------------------------------
# Filter 6 — Earnings & Macro Blackout Gate
# ---------------------------------------------------------------------------

class EarningsGateFilter:
    name = "earnings_gate"

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        ticker = market_data["ticker"]
        today: date = market_data.get("today") or date.today()

        blackout = {str(d) for d in config.get("blackout_dates", [])}
        if today.isoformat() in blackout:
            return FilterResult(
                passed=False, filter_name=self.name,
                reason=f"{today.isoformat()} is a configured macro blackout date (FOMC/CPI)",
                hard_stop=True,
            )

        buffer_days = int(config.get("earnings_buffer_days", 14))
        dte_min = int(config.get("dte_min", 30))
        dte_max = int(config.get("dte_max", 45))

        # Pre-fetched date may be injected for tests
        if "earnings_date" in market_data:
            earnings = market_data["earnings_date"]
        else:
            from src.earnings import next_earnings_date
            earnings = next_earnings_date(ticker)

        if earnings is None:
            if config.get("block_on_unknown_earnings", True):
                return FilterResult(
                    passed=False, filter_name=self.name,
                    reason="Earnings date unknown — unverified is not clear (blocked by config)",
                    hard_stop=True,
                )
            return FilterResult(
                passed=True, filter_name=self.name,
                reason="Earnings date unknown — allowed by config",
                adjustments={"soft_flag": "earnings_unverified"},
            )

        # Cap the expiry window so every candidate expiry clears the buffer:
        # a put expiring more than (earnings - buffer) out is not allowed.
        earnings_dte_cap = (earnings - timedelta(days=buffer_days) - today).days
        days_to_earnings = (earnings - today).days

        if earnings_dte_cap < dte_min:
            return FilterResult(
                passed=False, filter_name=self.name,
                reason=(
                    f"Earnings {earnings.isoformat()} in {days_to_earnings}d — even a "
                    f"{dte_min}DTE expiry lands inside the {buffer_days}d buffer"
                ),
                hard_stop=True,
                adjustments={"earnings_date": earnings.isoformat()},
            )

        dte_max_effective = min(dte_max, earnings_dte_cap)
        return FilterResult(
            passed=True, filter_name=self.name,
            reason=(
                f"Earnings {earnings.isoformat()} clear — expiry window capped at "
                f"{dte_max_effective}DTE (buffer {buffer_days}d)"
            ),
            adjustments={"earnings_date": earnings.isoformat(),
                         "dte_max_effective": dte_max_effective},
        )
