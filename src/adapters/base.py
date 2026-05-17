from datetime import date, datetime
from typing import Any, Protocol

import pandas as pd


class DataAdapter(Protocol):
    """Base protocol for market data adapters.

    All strategies use this interface. Methods added in Phase A4:
      - get_ohlcv now accepts optional timeframe ("1Day", "15Min", etc.)
      - get_quote returns current bid/ask/last
      - get_multi_ohlcv fetches multiple tickers in one call
    """

    def get_ohlcv(self, ticker: str, bars: int, timeframe: str = "1Day") -> pd.DataFrame:
        """Return OHLCV DataFrame with columns: open, high, low, close, volume; DatetimeIndex UTC."""
        ...

    def get_quote(self, ticker: str) -> dict[str, Any]:
        """Return current quote: {"bid": float, "ask": float, "last": float, "ts": datetime}."""
        ...

    def get_historical_ohlcv(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        """Return full OHLCV history for a date range; DatetimeIndex UTC. Always daily bars."""
        ...

    def get_multi_ohlcv(
        self,
        tickers: list[str],
        bars: int,
        timeframe: str = "1Day",
    ) -> dict[str, pd.DataFrame]:
        """Batched fetch — {ticker: df} for each ticker. Single API call when adapter supports it."""
        ...


class OptionsDataAdapter(DataAdapter, Protocol):
    """Extended adapter for options-based strategies (Wheel, PCS, condors)."""

    def get_options_chain(self, ticker: str, dte_min: int, dte_max: int) -> pd.DataFrame:
        """Return options chain with columns: strike, expiry, option_type, delta, iv, bid, ask."""
        ...

    def get_option_quote(self, occ_symbol: str) -> dict[str, Any]:
        """Return current option quote: {"bid": float, "ask": float, "iv": float, "delta": float}."""
        ...
