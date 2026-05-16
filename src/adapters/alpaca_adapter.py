import os
import structlog
from datetime import datetime, timezone, timedelta

import pandas as pd

import src.logger as _log_setup  # noqa: F401

logger = structlog.get_logger(__name__)


def _require_env(key: str) -> str:
    """Return env var value or raise a clear RuntimeError on missing key."""
    try:
        return os.environ[key]
    except KeyError:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Copy .env.example to .env and populate your Alpaca credentials."
        )


class AlpacaPaperAdapter:
    """
    Live adapter for Alpaca paper trading account.
    Requires ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL in environment.

    Phase 2 implementation — this stub provides the interface;
    full alpaca-py integration is wired in Phase 2.
    """

    def __init__(self):
        from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest, OptionChainRequest
        from alpaca.data.timeframe import TimeFrame

        api_key = _require_env("ALPACA_API_KEY")
        secret_key = _require_env("ALPACA_SECRET_KEY")

        self._stock_client = StockHistoricalDataClient(api_key, secret_key)
        self._option_client = OptionHistoricalDataClient(api_key, secret_key)
        self._TimeFrame = TimeFrame
        self._StockBarsRequest = StockBarsRequest
        self._OptionChainRequest = OptionChainRequest

    def get_ohlcv(self, ticker: str, bars: int) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = datetime.now(timezone.utc)
        # Request extra bars to account for weekends/holidays
        start = end - timedelta(days=bars * 2)

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars_response = self._stock_client.get_stock_bars(request)
        df = bars_response.df.loc[ticker] if ticker in bars_response.df.index.get_level_values(0) else pd.DataFrame()
        df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        result = df.iloc[-bars:]
        logger.info("ohlcv_fetched", ticker=ticker, bars=len(result))
        return result

    def get_options_chain(self, ticker: str, dte_min: int, dte_max: int) -> pd.DataFrame:
        import datetime as dt

        today = dt.date.today()
        expiry_start = today + dt.timedelta(days=dte_min)
        expiry_end = today + dt.timedelta(days=dte_max)

        request = self._OptionChainRequest(
            underlying_symbol=ticker,
            expiration_date_gte=expiry_start,
            expiration_date_lte=expiry_end,
        )
        chain = self._option_client.get_option_chain(request)
        if not chain:
            return pd.DataFrame()

        records = []
        for symbol, snapshot in chain.items():
            greeks = snapshot.greeks
            records.append({
                "symbol": symbol,
                "strike": snapshot.details.strike_price,
                "expiry": snapshot.details.expiration_date,
                "option_type": snapshot.details.type,
                "delta": greeks.delta if greeks else None,
                "iv": snapshot.implied_volatility,
                "bid": snapshot.latest_quote.bid_price if snapshot.latest_quote else None,
                "ask": snapshot.latest_quote.ask_price if snapshot.latest_quote else None,
                "volume": snapshot.day.volume if snapshot.day else None,
            })

        df = pd.DataFrame(records)
        today_ts = pd.Timestamp(today)
        df["dte"] = (pd.to_datetime(df["expiry"]) - today_ts).dt.days
        result = df.reset_index(drop=True)
        logger.info("options_chain_fetched", ticker=ticker, contracts=len(result))
        return result
