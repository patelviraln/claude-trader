"""
Signal output layer: Pydantic schema, rich terminal formatter, JSONL logger.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class IndicatorReadings(BaseModel):
    ema50: float
    ema50_slope: float
    rsi14: float
    bb_upper: float
    bb_lower: float
    bb_percent_b: float
    volume_node_nearest: float
    volume_node_type: Literal["support", "resistance"]


class SignalCard(BaseModel):
    ticker: str
    phase: Literal["SELL_PUT", "SELL_CALL", "NO_SIGNAL"]
    signal_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    underlying_price: float
    recommended_strike: float | None = None
    recommended_expiry: str | None = None
    dte: int | None = None
    delta_estimate: float | None = None
    iv_rank: float | None = None
    indicator_readings: IndicatorReadings | None = None
    thesis_text: str = ""
    confidence_tier: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    confidence_rationale: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    no_signal_reason: str | None = None

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
        reasons.append("All 6 filters passed cleanly")

    return tier, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Rich Terminal Formatter
# ---------------------------------------------------------------------------

_TIER_COLORS = {
    "HIGH": "bold green",
    "MEDIUM": "bold yellow",
    "LOW": "bold red",
}

_PHASE_COLORS = {
    "SELL_PUT": "cyan",
    "SELL_CALL": "magenta",
    "NO_SIGNAL": "dim",
}

_console = Console(stderr=False)


def print_signal_card(card: SignalCard) -> None:
    """Render a signal card to the terminal using rich."""
    phase_color = _PHASE_COLORS.get(card.phase, "white")
    tier_color = _TIER_COLORS.get(card.confidence_tier or "", "white")

    # Header line
    header = f"[bold]{card.ticker}[/bold]  [{phase_color}]{card.phase}[/{phase_color}]"
    if card.confidence_tier:
        header += f"  [{tier_color}]{card.confidence_tier}[/{tier_color}]"

    if card.phase == "NO_SIGNAL":
        panel = Panel(
            f"[dim]{card.no_signal_reason}[/dim]",
            title=header,
            border_style="dim",
        )
        _console.print(panel)
        return

    # Build details table
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Field", style="dim", width=22)
    table.add_column("Value")

    if card.recommended_strike is not None:
        table.add_row("Strike", f"${card.recommended_strike:.2f}")
    if card.recommended_expiry:
        table.add_row("Expiry", f"{card.recommended_expiry}  (DTE {card.dte})")
    if card.delta_estimate is not None:
        table.add_row("Delta", f"{card.delta_estimate:+.3f}")
    if card.iv_rank is not None:
        table.add_row("IV Rank", f"{card.iv_rank:.1f}%")
    table.add_row("Underlying", f"${card.underlying_price:.2f}")

    if card.indicator_readings:
        ir = card.indicator_readings
        table.add_row("EMA50", f"{ir.ema50:.2f}  (slope {ir.ema50_slope:+.4f})")
        table.add_row("RSI14", f"{ir.rsi14:.1f}")
        table.add_row("BB %B", f"{ir.bb_percent_b:.2f}")
        table.add_row("HVN", f"${ir.volume_node_nearest:.2f}  ({ir.volume_node_type})")

    if card.thesis_text:
        table.add_row("Thesis", card.thesis_text)
    if card.confidence_rationale:
        table.add_row("Rationale", card.confidence_rationale)
    if card.risk_flags:
        table.add_row("[yellow]Risk flags[/yellow]", ", ".join(card.risk_flags))

    panel = Panel(table, title=header, border_style=phase_color)
    _console.print(panel)


# ---------------------------------------------------------------------------
# JSONL Logger
# ---------------------------------------------------------------------------

def append_signal_jsonl(card: SignalCard, path: str | Path = "signals.jsonl") -> None:
    """Append the signal card as a single JSON line to the JSONL file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(card.to_json() + "\n")
