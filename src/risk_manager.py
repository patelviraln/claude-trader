"""
Phase 10 — risk management & position sizing.

Every order the system wants to place passes through RiskManager.validate()
first. A signal without sizing logic is dangerous: one cash-secured NVDA put
at a $840 strike ties up $84,000 of collateral. The manager enforces:

  max_position_pct        — capital per new position as % of account equity
  max_positions_total     — cap on concurrent open positions
  max_positions_per_ticker— cap per underlying (default 1)
  min_buying_power_pct    — % of equity that must remain free after the trade

Capital required per unit, by signal type:
  SELL_PUT          strike x 100          (cash-secured collateral)
  SELL_PUT_SPREAD   (width - credit) x 100 (defined max loss)
  BUY_EQUITY        limit_price x qty
  SELL_CALL         0  (covered by the 100 shares already held)
  SELL_EQUITY       0  (closing an existing position)

Config lives in [risk] of config/strategies.toml. enabled=false approves
everything (logged), individual limits of 0 disable that single check.
"""
from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401

if TYPE_CHECKING:
    from src.positions import Position
    from src.signal_output import SignalCard

logger = structlog.get_logger(__name__)

DEFAULT_RISK: dict[str, Any] = {
    "enabled": True,
    "max_position_pct": 5.0,
    "max_positions_total": 10,
    "max_positions_per_ticker": 1,
    "min_buying_power_pct": 20.0,
}

# Closing/covered actions never consume new capital
_NO_CAPITAL_SIGNALS = {"SELL_CALL", "SELL_EQUITY"}


def load_risk_rules(config_path: str | Path = "config/strategies.toml") -> dict[str, Any]:
    """Read [risk] from a config TOML, falling back to defaults."""
    p = Path(config_path)
    if not p.exists():
        return dict(DEFAULT_RISK)
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    return {**DEFAULT_RISK, **raw.get("risk", {})}


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    approved_qty: int = 0          # may be smaller than requested (downsized)
    requested_qty: int = 0
    required_capital: float = 0.0  # for approved_qty
    checks: list[str] = []


def capital_per_unit(card: "SignalCard") -> float:
    """Capital consumed by ONE contract/share-lot of this signal."""
    if card.signal_type in _NO_CAPITAL_SIGNALS or not card.legs:
        return 0.0

    leg0 = card.legs[0]
    if card.signal_type == "SELL_PUT":
        return (leg0.strike or card.underlying_price) * 100

    if card.signal_type == "SELL_PUT_SPREAD" and len(card.legs) >= 2:
        width = abs((card.legs[0].strike or 0.0) - (card.legs[1].strike or 0.0))
        credit = float(card.payload.get("net_credit") or leg0.limit_price or 0.0)
        return max(width - credit, 0.0) * 100

    if card.signal_type == "BUY_EQUITY":
        return float(leg0.limit_price or card.underlying_price)

    # Unknown actionable type — be conservative: full underlying notional
    return card.underlying_price * 100


class RiskManager:
    """Validates orders against account state and configured limits."""

    def __init__(
        self,
        rules: dict[str, Any] | None = None,
        account: dict[str, Any] | None = None,
        open_positions: "list[Position] | None" = None,
    ):
        self.rules = {**DEFAULT_RISK, **(rules or {})}
        self.account = account or {}
        self.open_positions = open_positions or []

    # ------------------------------------------------------------------

    def validate(self, card: "SignalCard") -> RiskDecision:
        rules = self.rules
        requested = int(card.legs[0].qty) if card.legs else 0
        checks: list[str] = []

        if not rules.get("enabled", True):
            return RiskDecision(approved=True, reason="risk checks disabled",
                                approved_qty=requested, requested_qty=requested,
                                checks=["disabled"])

        if card.signal_type == "NO_SIGNAL" or not card.legs:
            return RiskDecision(approved=False, reason="nothing to trade",
                                requested_qty=0)

        # Closing / covered trades are always allowed
        if card.signal_type in _NO_CAPITAL_SIGNALS:
            return RiskDecision(approved=True, reason="closing/covered trade — no new capital",
                                approved_qty=requested, requested_qty=requested,
                                checks=["no_capital_required"])

        equity = float(self.account.get("equity") or 0.0)
        bp = float(
            self.account.get("options_buying_power")
            or self.account.get("buying_power")
            or 0.0
        )
        if equity <= 0:
            return RiskDecision(approved=False, requested_qty=requested,
                                reason="account equity unavailable — refusing to size blind")

        # --- Concentration: per-ticker ---
        per_ticker_cap = int(rules.get("max_positions_per_ticker", 1))
        if per_ticker_cap > 0:
            held_here = sum(1 for p in self.open_positions if p.underlying == card.ticker)
            if held_here >= per_ticker_cap:
                return RiskDecision(
                    approved=False, requested_qty=requested,
                    reason=f"concentration: {held_here} open position(s) on {card.ticker} "
                           f">= per-ticker cap {per_ticker_cap}")
            checks.append(f"per_ticker {held_here}/{per_ticker_cap}")

        # --- Concentration: total ---
        total_cap = int(rules.get("max_positions_total", 10))
        if total_cap > 0:
            total_open = len(self.open_positions)
            if total_open >= total_cap:
                return RiskDecision(
                    approved=False, requested_qty=requested,
                    reason=f"concentration: {total_open} open positions >= total cap {total_cap}")
            checks.append(f"total {total_open}/{total_cap}")

        # --- Position sizing: % of equity per position ---
        unit_capital = capital_per_unit(card)
        if unit_capital <= 0:
            return RiskDecision(approved=False, requested_qty=requested,
                                reason="cannot determine capital requirement")

        max_pct = float(rules.get("max_position_pct", 5.0))
        if max_pct > 0:
            budget = equity * max_pct / 100.0
            affordable = math.floor(budget / unit_capital)
        else:  # sizing check disabled
            budget = float("inf")
            affordable = requested
        qty = min(requested, affordable) if affordable > 0 else 0
        if qty < 1:
            return RiskDecision(
                approved=False, requested_qty=requested,
                reason=f"sizing: one unit needs ${unit_capital:,.0f} but "
                       f"{max_pct:g}% of equity is ${budget:,.0f}")
        if qty < requested:
            checks.append(f"downsized {requested}->{qty}")
        checks.append(f"size ${qty * unit_capital:,.0f} <= budget ${budget:,.0f}")

        # --- Buying power reserve ---
        required = qty * unit_capital
        reserve_pct = float(rules.get("min_buying_power_pct", 20.0))
        reserve = equity * reserve_pct / 100.0 if reserve_pct > 0 else 0.0
        if required > bp - reserve:
            return RiskDecision(
                approved=False, requested_qty=requested,
                reason=f"buying power: needs ${required:,.0f} but only "
                       f"${max(bp - reserve, 0):,.0f} free after {reserve_pct:g}% reserve "
                       f"(BP ${bp:,.0f})")
        checks.append(f"bp ${required:,.0f} <= free ${bp - reserve:,.0f}")

        return RiskDecision(approved=True, reason="within limits",
                            approved_qty=qty, requested_qty=requested,
                            required_capital=round(required, 2), checks=checks)


def validate_order(card: "SignalCard", executor, rules: dict[str, Any] | None = None) -> RiskDecision:
    """Fetch live account + positions from the executor and validate one card."""
    from src.positions import fetch_positions

    rules = rules if rules is not None else load_risk_rules()
    try:
        account = executor.get_account()
        positions = fetch_positions(executor)
    except Exception as exc:
        logger.error("risk.account_fetch_failed", error=str(exc))
        return RiskDecision(approved=False, requested_qty=0,
                            reason=f"account state unavailable: {exc}")

    decision = RiskManager(rules, account, positions).validate(card)
    logger.info("risk.validated", ticker=card.ticker, signal_type=card.signal_type,
                approved=decision.approved, qty=decision.approved_qty,
                reason=decision.reason)
    return decision
