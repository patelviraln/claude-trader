"""
Seven wheel strategy filter classes. Each returns a FilterResult.
Filter order matches the pipeline: Phase Gate → EMA → RSI → BB → Options → Delta → Volume.
"""
from __future__ import annotations

import datetime
from typing import Any, Literal

import pandas as pd

from src.indicator_engine import (
    BollingerResult,
    EMAResult,
    RSIResult,
    VolumeProfileResult,
    compute_bollinger,
    compute_ema,
    compute_rsi,
    compute_volume_profile,
)
from src.strategies.base import FilterResult
from src.strategies.wheel.state import WheelState


# ---------------------------------------------------------------------------
# Filter 1 — Phase Gate (O(1), no I/O)
# ---------------------------------------------------------------------------

class PhaseGateFilter:
    name = "phase_gate"

    def run(self, ticker: str, state: WheelState) -> FilterResult:
        if state.phase not in ("A", "B"):
            return FilterResult(
                passed=False,
                filter_name=self.name,
                reason=f"Unknown phase '{state.phase}' for {ticker}",
                hard_stop=True,
            )
        return FilterResult(
            passed=True,
            filter_name=self.name,
            reason=f"Phase {state.phase} — proceeding",
            adjustments={"phase": state.phase},
        )


# ---------------------------------------------------------------------------
# Filter 2 — EMA Trend Filter (HARD STOP if failing)
# ---------------------------------------------------------------------------

class EMATrendFilter:
    name = "ema_trend"

    def __init__(self, ema_period: int = 50):
        self.ema_period = ema_period

    def run(self, df: pd.DataFrame) -> FilterResult:
        result: EMAResult = compute_ema(df, self.ema_period)
        current_close = float(df["close"].iloc[-1])
        price_above = current_close > result["ema"]
        slope_positive = result["slope"] > 0

        if not (price_above and slope_positive):
            reasons = []
            if not price_above:
                reasons.append(f"price {current_close:.2f} below EMA{self.ema_period} {result['ema']:.2f}")
            if not slope_positive:
                reasons.append(f"EMA slope {result['slope']:.4f} ≤ 0 (declining)")
            return FilterResult(
                passed=False,
                filter_name=self.name,
                reason="; ".join(reasons),
                hard_stop=True,
            )

        return FilterResult(
            passed=True,
            filter_name=self.name,
            reason=f"Price {current_close:.2f} above rising EMA{self.ema_period} {result['ema']:.2f} (slope={result['slope']:.4f})",
            adjustments={"ema50": result["ema"], "ema50_slope": result["slope"]},
        )


# ---------------------------------------------------------------------------
# Filter 3 — RSI Gate (HARD STOP if out of range)
# ---------------------------------------------------------------------------

class RSIFilter:
    name = "rsi_gate"

    def __init__(self, rsi_min: float = 35.0, rsi_max: float = 65.0, period: int = 14):
        self.rsi_min = rsi_min
        self.rsi_max = rsi_max
        self.period = period

    def run(self, df: pd.DataFrame) -> FilterResult:
        result: RSIResult = compute_rsi(df, self.period)
        rsi = result["rsi"]

        if not (self.rsi_min <= rsi <= self.rsi_max):
            direction = "overbought" if rsi > self.rsi_max else "oversold"
            return FilterResult(
                passed=False,
                filter_name=self.name,
                reason=f"RSI {rsi:.1f} out of range [{self.rsi_min}, {self.rsi_max}] — {direction}",
                hard_stop=True,
            )

        return FilterResult(
            passed=True,
            filter_name=self.name,
            reason=f"RSI {rsi:.1f} within neutral range [{self.rsi_min}, {self.rsi_max}]",
            adjustments={"rsi14": rsi},
        )


# ---------------------------------------------------------------------------
# Filter 4 — Bollinger Band Position (SOFT FLAG)
# ---------------------------------------------------------------------------

class BollingerFilter:
    name = "bb_position"
    FAVORABLE_THRESHOLD = 0.20  # lower 20% of band = favorable for put sell

    def __init__(self, period: int = 20, std_dev: float = 2.0):
        self.period = period
        self.std_dev = std_dev

    def run(self, df: pd.DataFrame, phase: Literal["A", "B"]) -> FilterResult:
        result: BollingerResult = compute_bollinger(df, self.period, self.std_dev)
        pct_b = result["percent_b"]

        if phase == "A":
            favorable = pct_b <= self.FAVORABLE_THRESHOLD
            position_label = f"%B={pct_b:.2f} ({'favorable' if favorable else 'unfavorable'} for put sell)"
        else:
            favorable = pct_b >= (1.0 - self.FAVORABLE_THRESHOLD)
            position_label = f"%B={pct_b:.2f} ({'favorable' if favorable else 'unfavorable'} for call sell)"

        adjustments = {
            "bb_upper": result["upper"],
            "bb_lower": result["lower"],
            "bb_mid": result["mid"],
            "bb_percent_b": pct_b,
            "bb_favorable": favorable,
        }

        if not favorable:
            return FilterResult(
                passed=True,  # soft — does not block
                filter_name=self.name,
                reason=f"BB position unfavorable — {position_label}",
                hard_stop=False,
                adjustments={**adjustments, "soft_flag": "bb_unfavorable"},
            )

        return FilterResult(
            passed=True,
            filter_name=self.name,
            reason=f"BB position favorable — {position_label}",
            adjustments=adjustments,
        )


# ---------------------------------------------------------------------------
# Filter 5 — Options Chain Availability (HARD STOP if no data)
# ---------------------------------------------------------------------------

class OptionsChainFilter:
    name = "options_chain"

    def run(self, options_chain: pd.DataFrame) -> FilterResult:
        if options_chain.empty:
            return FilterResult(
                passed=False,
                filter_name=self.name,
                reason="No options chain data returned for requested DTE window",
                hard_stop=True,
            )

        required_cols = {"strike", "expiry", "delta", "iv"}
        missing = required_cols - set(options_chain.columns)
        if missing:
            return FilterResult(
                passed=False,
                filter_name=self.name,
                reason=f"Options chain missing required columns: {missing}",
                hard_stop=True,
            )

        return FilterResult(
            passed=True,
            filter_name=self.name,
            reason=f"Options chain available — {len(options_chain)} contracts",
        )


# ---------------------------------------------------------------------------
# Filter 6 — Delta Targeting (HARD STOP if no match in tolerance)
# ---------------------------------------------------------------------------

class DeltaTargetFilter:
    name = "delta_target"

    def __init__(self, delta_target: float = 0.30, delta_tolerance: float = 0.05):
        self.delta_target = delta_target
        self.delta_tolerance = delta_tolerance

    def run(
        self,
        options_chain: pd.DataFrame,
        phase: Literal["A", "B"],
    ) -> FilterResult:
        option_type = "put" if phase == "A" else "call"
        target_sign = -1 if phase == "A" else 1
        target_delta = target_sign * self.delta_target

        chain = options_chain[options_chain["option_type"] == option_type].copy()
        if chain.empty:
            return FilterResult(
                passed=False,
                filter_name=self.name,
                reason=f"No {option_type} options in chain",
                hard_stop=True,
            )

        chain["delta_distance"] = (chain["delta"] - target_delta).abs()
        best = chain.loc[chain["delta_distance"].idxmin()]
        actual_delta = float(best["delta"])
        distance = float(best["delta_distance"])

        if distance > self.delta_tolerance:
            return FilterResult(
                passed=False,
                filter_name=self.name,
                reason=(
                    f"No {option_type} strike within delta tolerance ±{self.delta_tolerance}. "
                    f"Closest: delta={actual_delta:.3f} at strike {best['strike']:.2f} "
                    f"(distance={distance:.3f})"
                ),
                hard_stop=True,
            )

        expiry = best["expiry"]
        dte = int(best["dte"]) if "dte" in best.index else None

        return FilterResult(
            passed=True,
            filter_name=self.name,
            reason=(
                f"Delta match: {option_type} strike {best['strike']:.2f}, "
                f"delta={actual_delta:.3f}, expiry={expiry}, DTE={dte}"
            ),
            adjustments={
                "recommended_strike": float(best["strike"]),
                "recommended_expiry": str(expiry),
                "delta_estimate": actual_delta,
                "iv": float(best["iv"]) if pd.notna(best.get("iv")) else None,
                "dte": dte,
            },
        )


# ---------------------------------------------------------------------------
# Filter 7 — Volume Profile Anchor (SOFT FLAG)
# ---------------------------------------------------------------------------

class VolumeProfileFilter:
    name = "volume_profile"
    DIVERGENCE_THRESHOLD = 0.05  # 5% from strike

    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    def run(
        self,
        df: pd.DataFrame,
        phase: Literal["A", "B"],
        recommended_strike: float,
    ) -> FilterResult:
        result: VolumeProfileResult = compute_volume_profile(df, self.lookback)
        hvn = result["hvn_price"]
        hvn_type = result["hvn_type"]

        divergence = abs(recommended_strike - hvn) / hvn if hvn != 0 else 0.0
        diverges = divergence > self.DIVERGENCE_THRESHOLD

        adjustments = {
            "volume_node_nearest": hvn,
            "volume_node_type": hvn_type,
            "hvn_divergence_pct": round(divergence * 100, 2),
        }

        if phase == "A":
            # Put sell: strike should be BELOW nearest HVN support
            anchor_ok = recommended_strike < hvn and hvn_type == "support"
        else:
            # Call sell: strike should be NEAR HVN ceiling (resistance)
            anchor_ok = hvn_type == "resistance" and not diverges

        if not anchor_ok or diverges:
            return FilterResult(
                passed=True,  # soft — does not block
                filter_name=self.name,
                reason=(
                    f"Volume profile divergence {divergence*100:.1f}% from strike {recommended_strike:.2f}. "
                    f"HVN at {hvn:.2f} ({hvn_type})"
                ),
                hard_stop=False,
                adjustments={**adjustments, "soft_flag": "volume_node_divergence"},
            )

        return FilterResult(
            passed=True,
            filter_name=self.name,
            reason=f"Strike {recommended_strike:.2f} anchored by HVN {hvn:.2f} ({hvn_type})",
            adjustments=adjustments,
        )
