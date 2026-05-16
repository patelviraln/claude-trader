from datetime import date
from typing import Protocol
import pandas as pd


class DataAdapter(Protocol):
    def get_ohlcv(self, ticker: str, bars: int) -> pd.DataFrame:
        """Return OHLCV DataFrame with columns: open, high, low, close, volume; DatetimeIndex UTC."""
        ...

    def get_options_chain(self, ticker: str, dte_min: int, dte_max: int) -> pd.DataFrame:
        """Return options chain DataFrame with columns: strike, expiry, option_type, delta, iv, bid, ask, volume."""
        ...

    def get_historical_ohlcv(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        """Return full OHLCV history for a date range; DatetimeIndex UTC."""
        ...
