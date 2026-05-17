from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import pandas as pd
from pydantic import BaseModel

if TYPE_CHECKING:
    from src.adapters.base import DataAdapter
    from src.signal_output import SignalCard


class FilterResult(BaseModel):
    passed: bool
    filter_name: str
    reason: str
    hard_stop: bool = False
    adjustments: dict[str, Any] = {}


class Filter(Protocol):
    name: str

    def run(self, market_data: dict, state: BaseModel, config: dict) -> FilterResult:
        ...


class Strategy(Protocol):
    name: str

    def load_config(self, config: dict[str, Any]) -> None:
        """Load strategy parameters from a config dict."""
        ...

    def get_state_schema(self) -> type[BaseModel]:
        """Return the Pydantic model class for this strategy's per-ticker state."""
        ...

    def emit_signal_card(
        self,
        ticker: str,
        adapter: "DataAdapter",
        state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> "SignalCard":
        """Run the full pipeline and return a strategy-shaped SignalCard.
        context may carry runtime overrides (e.g. {"iv_history_dir": Path(...)}).
        """
        ...

    def evaluate(
        self,
        ticker: str,
        ohlcv: pd.DataFrame,
        options_chain: pd.DataFrame,
        state: dict[str, Any],
    ) -> list[FilterResult]:
        """Run the filter chain and return ordered FilterResult list."""
        ...
