"""RSI2Strategy per-ticker state schema."""
from __future__ import annotations

from pydantic import BaseModel


class RSI2State(BaseModel):
    in_position: bool = False
    entry_date: str | None = None   # YYYY-MM-DD
    entry_price: float | None = None
    shares: int = 0

    @classmethod
    def default(cls) -> "RSI2State":
        return cls()

    @classmethod
    def from_dict(cls, d: dict) -> "RSI2State":
        return cls(**{k: v for k, v in d.items() if k in cls.model_fields})
