"""PutCreditSpreadStrategy per-ticker state schema."""
from __future__ import annotations

from pydantic import BaseModel


class PCSState(BaseModel):
    open_spread: bool = False
    short_strike: float | None = None
    long_strike: float | None = None
    expiry: str | None = None      # YYYY-MM-DD
    entry_credit: float | None = None
    entry_date: str | None = None

    @classmethod
    def default(cls) -> "PCSState":
        return cls()

    @classmethod
    def from_dict(cls, d: dict) -> "PCSState":
        return cls(**{k: v for k, v in d.items() if k in cls.model_fields})
