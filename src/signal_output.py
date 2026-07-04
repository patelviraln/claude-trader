"""
Signal output layer: Pydantic schema, rich terminal formatter, JSONL logger.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class Leg(BaseModel):
    """One leg of a trade — equity or single option contract."""
    asset_class: Literal["equity", "option"]
    symbol: str  # OCC for options, ticker for equity
    side: Literal["buy", "sell"]
    qty: int  # contracts (option) or shares (equity)
    order_type: Literal["market", "limit"] = "limit"
    limit_price: float | None = None
    # Option-specific (None for equity legs):
    strike: float | None = None
    expiry: str | None = None  # YYYY-MM-DD
    option_type: Literal["put", "call"] | None = None
    delta_estimate: float | None = None


class SignalCard(BaseModel):
    strategy_name: str  # e.g. "wheel", "put_credit_spread", "rsi2"
    ticker: str
    signal_type: str  # strategy-defined; "SELL_PUT", "SELL_CALL", "NO_SIGNAL", "SELL_PUT_SPREAD", "BUY_EQUITY"
    signal_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    underlying_price: float
    legs: list[Leg] = Field(default_factory=list)  # empty when signal_type == "NO_SIGNAL"
    indicators: dict[str, Any] = Field(default_factory=dict)  # strategy-specific readings
    payload: dict[str, Any] = Field(default_factory=dict)  # extras: iv_rank, dte, spread_width, ...
    thesis_text: str = ""
    confidence_tier: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    confidence_rationale: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    no_signal_reason: str | None = None
    max_contracts: int | None = None  # risk-approved quantity (Phase 10); None = not evaluated
    order_ids: list[str] = Field(default_factory=list)
    order_statuses: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(indent=None)


# ---------------------------------------------------------------------------
# Confidence Tier Logic
# ---------------------------------------------------------------------------

_TIER_HIGH = "HIGH"
_TIER_MEDIUM = "MEDIUM"
_TIER_LOW = "LOW"


def compute_confidence_tier(
    soft_flags: list[str],
    delta_distance: float,
    iv_rank: float | None,
) -> tuple[str, str]:
    """Return (tier, rationale) based on soft flags, delta precision, IV availability."""
    reasons: list[str] = []
    tier = _TIER_HIGH

    if len(soft_flags) >= 2:
        tier = _TIER_LOW
        reasons.append(f"{len(soft_flags)} soft flags tripped: {', '.join(soft_flags)}")
    elif len(soft_flags) == 1:
        tier = _TIER_MEDIUM
        reasons.append(f"Soft flag: {soft_flags[0]}")

    if delta_distance > 0.05:
        tier = _TIER_LOW
        reasons.append(f"Delta at tolerance edge (distance={delta_distance:.3f})")
    elif delta_distance > 0.02 and tier == _TIER_HIGH:
        tier = _TIER_MEDIUM
        reasons.append(f"Delta within tolerance but not precise (distance={delta_distance:.3f})")

    if iv_rank is None:
        if tier == _TIER_HIGH:
            tier = _TIER_MEDIUM
        reasons.append("IV rank unavailable")

    if not reasons:
        reasons.append("All filters passed cleanly")

    return tier, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Rich Terminal Formatter
# ---------------------------------------------------------------------------

_TIER_COLORS = {
    "HIGH": "bold green",
    "MEDIUM": "bold yellow",
    "LOW": "bold red",
}

_SIGNAL_COLORS = {
    "SELL_PUT": "cyan",
    "SELL_CALL": "magenta",
    "NO_SIGNAL": "dim",
    "SELL_PUT_SPREAD": "blue",
    "BUY_EQUITY": "green",
    "SELL_EQUITY": "red",
}

# Indicators rendered via grouped display; all others printed key=value
_GROUPED_INDICATORS = frozenset(
    {"ema50", "ema50_slope", "rsi14", "bb_percent_b", "volume_node_nearest", "volume_node_type"}
)

_console = Console(stderr=False)


def print_signal_card(card: SignalCard) -> None:
    """Render a signal card to the terminal using rich."""
    sig_color = _SIGNAL_COLORS.get(card.signal_type, "white")
    tier_color = _TIER_COLORS.get(card.confidence_tier or "", "white")

    header = f"[bold]{card.ticker}[/bold]  [{sig_color}]{card.signal_type}[/{sig_color}]"
    if card.confidence_tier:
        header += f"  [{tier_color}]{card.confidence_tier}[/{tier_color}]"

    if card.signal_type == "NO_SIGNAL":
        _console.print(Panel(
            f"[dim]{card.no_signal_reason}[/dim]",
            title=header,
            border_style="dim",
        ))
        return

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Field", style="dim", width=22)
    table.add_column("Value")

    table.add_row("Underlying", f"${card.underlying_price:.2f}")

    multi = len(card.legs) > 1
    for i, leg in enumerate(card.legs):
        prefix = f"Leg {i + 1} " if multi else ""
        side_str = "SELL" if leg.side == "sell" else "BUY"
        if leg.asset_class == "option" and leg.strike is not None:
            table.add_row(f"{prefix}Strike", f"${leg.strike:.2f}  ({side_str})")
        if leg.expiry:
            dte_suffix = ""
            if i == 0 and card.payload.get("dte") is not None:
                dte_suffix = f"  (DTE {card.payload['dte']})"
            table.add_row(f"{prefix}Expiry", f"{leg.expiry}{dte_suffix}")
        if leg.delta_estimate is not None:
            table.add_row(f"{prefix}Delta", f"{leg.delta_estimate:+.3f}")
        if leg.limit_price is not None:
            label = "Option Mid" if leg.asset_class == "option" else "Limit"
            table.add_row(f"{prefix}{label}", f"${leg.limit_price:.2f}")

    # Payload extras
    if card.payload.get("iv_rank") is not None:
        table.add_row("IV Rank", f"{card.payload['iv_rank']:.1f}%")
    if card.payload.get("max_profit") is not None:
        table.add_row("Max Profit", f"${card.payload['max_profit']:.2f}")
    if card.payload.get("max_loss") is not None:
        table.add_row("Max Loss", f"${card.payload['max_loss']:.2f}")

    # Grouped Wheel indicators
    inds = card.indicators
    if "ema50" in inds:
        slope = inds.get("ema50_slope", 0.0)
        table.add_row("EMA50", f"{inds['ema50']:.2f}  (slope {slope:+.4f})")
    if "rsi14" in inds:
        table.add_row("RSI14", f"{inds['rsi14']:.1f}")
    if "bb_percent_b" in inds:
        table.add_row("BB %B", f"{inds['bb_percent_b']:.2f}")
    if "volume_node_nearest" in inds:
        node_type = inds.get("volume_node_type", "?")
        table.add_row("HVN", f"${inds['volume_node_nearest']:.2f}  ({node_type})")
    # Any remaining non-grouped indicators
    for key, val in inds.items():
        if key not in _GROUPED_INDICATORS:
            label = key.replace("_", " ").title()
            table.add_row(label, f"{val:.4f}" if isinstance(val, float) else str(val))

    if card.thesis_text:
        table.add_row("Thesis", card.thesis_text)
    if card.confidence_rationale:
        table.add_row("Rationale", card.confidence_rationale)
    if card.risk_flags:
        table.add_row("[yellow]Risk flags[/yellow]", ", ".join(card.risk_flags))
    for oid, ost in zip(card.order_ids, card.order_statuses):
        table.add_row("[bold green]Order ID[/bold green]", f"{oid}  [{ost}]")

    _console.print(Panel(table, title=header, border_style=sig_color))


# ---------------------------------------------------------------------------
# JSONL Logger
# ---------------------------------------------------------------------------

def append_signal_jsonl(card: SignalCard, path: str | Path = "signals.jsonl") -> None:
    """Append the signal card as a single JSON line to the JSONL file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(card.to_json() + "\n")
