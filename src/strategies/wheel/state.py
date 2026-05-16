from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, Field


class WheelState(BaseModel):
    phase: Literal["A", "B"] = "A"
    shares_held: int = 0
    cost_basis: float | None = None
    assignment_date: datetime.date | None = None
    open_put_strike: float | None = None
    open_put_expiry: datetime.date | None = None

    @classmethod
    def default(cls) -> WheelState:
        return cls()

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict) -> WheelState:
        return cls.model_validate(data)
