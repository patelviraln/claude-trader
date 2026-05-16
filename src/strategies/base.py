from typing import Any, Protocol
import pandas as pd
from pydantic import BaseModel


class FilterResult(BaseModel):
    passed: bool
    filter_name: str
    reason: str
    hard_stop: bool = False
    adjustments: dict[str, Any] = {}


class Strategy(Protocol):
    name: str

    def load_config(self, config: dict[str, Any]) -> None:
        """Load strategy parameters from a config dict."""
        ...

    def get_state_schema(self) -> type[BaseModel]:
        """Return the Pydantic model class for this strategy's per-ticker state."""
        ...

    def evaluate(
        self,
        ticker: str,
        ohlcv: pd.DataFrame,
        options_chain: pd.DataFrame,
        state: dict[str, Any],
    ) -> list[FilterResult]:
        """Run the full filter chain and return ordered FilterResult list."""
        ...
