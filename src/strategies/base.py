from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

import pandas as pd
from pydantic import BaseModel

if TYPE_CHECKING:
    from src.adapters.base import DataAdapter
    from src.signal_output import SignalCard


class TradeResult(BaseModel):
    """Outcome of a single simulated or live trade."""
    entry_date: date
    exit_date: date
    signal_type: str  # e.g. "SELL_PUT", "SELL_CALL", "BUY_EQUITY"
    ticker: str
    pnl: float  # realized P&L in dollars (after frictions if modelled)
    metadata: dict[str, Any] = {}


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

    def simulate_trade(
        self,
        card: "SignalCard",
        future_ohlcv: pd.DataFrame,
        future_options_chains: "dict[date, pd.DataFrame] | None" = None,
    ) -> "TradeResult":
        """Given an emitted card and future market data, return the realized trade outcome.
        Each strategy implements its own P&L model (BS for options, bar-for-bar for equity).
        """
        ...
