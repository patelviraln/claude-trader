"""RedDayCSPStrategy per-ticker state schema."""
from __future__ import annotations

from pydantic import BaseModel


class RedDayState(BaseModel):
    # "phase" is fixed at "A" so wheel's DeltaTargetFilter (which selects puts
    # for phase A) can be reused unchanged. red_day_csp never sells calls.
    phase: str = "A"
    open_put_strike: float | None = None
    open_put_expiry: str | None = None   # YYYY-MM-DD
    entry_date: str | None = None        # YYYY-MM-DD
    last_scan_verdict: str | None = None  # TRADE / blocked-filter name
    last_scan_date: str | None = None

    @classmethod
    def default(cls) -> "RedDayState":
        return cls()

    @classmethod
    def from_dict(cls, d: dict) -> "RedDayState":
        return cls(**{k: v for k, v in d.items() if k in cls.model_fields})
