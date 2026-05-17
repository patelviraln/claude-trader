import os
import structlog
import datetime as dt
from datetime import date, datetime, timezone, timedelta
from typing import Any

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    OptionChainRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import src.logger as _log_setup  # noqa: F401

logger = structlog.get_logger(__name__)

# Lazy-import the quote request classes (they may not exist in older SDK versions)
try:
    from alpaca.data.requests import StockLatestQuoteRequest
    _HAS_STOCK_QUOTE = True
except ImportError:
    _HAS_STOCK_QUOTE = False

try:
    from alpaca.data.requests import OptionLatestQuoteRequest
    _HAS_OPTION_QUOTE = True
except ImportError:
    _HAS_OPTION_QUOTE = False

_TIMEFRAME_MAP: dict[str, Any] = {
    "1Day":  TimeFrame.Day,
    "1Hour": TimeFrame.Hour,
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "5Min":  TimeFrame(5, TimeFrameUnit.Minute),
    "1Min":  TimeFrame.Minute,
}


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
    Requires ALPACA_API_KEY, ALPACA_SECRET_KEY in environment.
    """

    def __init__(self):
        api_key = _require_env("ALPACA_API_KEY")
        secret_key = _require_env("ALPACA_SECRET_KEY")

        self._stock_client = StockHistoricalDataClient(api_key, secret_key)
        self._option_client = OptionHistoricalDataClient(api_key, secret_key)

    def get_ohlcv(self, ticker: str, bars: int, timeframe: str = "1Day") -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        # For intraday bars fetch fewer calendar days; for daily, go back 2× bars
        lookback_days = bars * 2 if timeframe == "1Day" else max(bars // 6, 5)
        start = end - timedelta(days=lookback_days)
        tf = _TIMEFRAME_MAP.get(timeframe, TimeFrame.Day)

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=tf,
            start=start,
            end=end,
            feed="iex",
        )
        bars_response = self._stock_client.get_stock_bars(request)  # type: ignore[arg-type]

        try:
            levels = bars_response.df.index.get_level_values(0)
            if ticker not in levels:
                raise RuntimeError(f"No OHLCV data returned for {ticker} — check ticker symbol")
            df = bars_response.df.loc[ticker]
        except KeyError:
            raise RuntimeError(f"No OHLCV data returned for {ticker} — check ticker symbol")

        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        result = df.iloc[-bars:]

        if len(result) < bars // 2:
            raise RuntimeError(
                f"Insufficient OHLCV bars for {ticker}: got {len(result)}, need at least {bars // 2}"
            )
        logger.info("ohlcv_fetched", ticker=ticker, bars=len(result), timeframe=timeframe)
        return result

    def get_quote(self, ticker: str) -> dict[str, Any]:
        """Return latest bid/ask/last from Alpaca IEX feed."""
        if not _HAS_STOCK_QUOTE:
            logger.warning("get_quote_unavailable", reason="StockLatestQuoteRequest not in SDK")
            return {"bid": 0.0, "ask": 0.0, "last": 0.0, "ts": datetime.now(timezone.utc)}

        req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed="iex")
        resp = self._stock_client.get_stock_latest_quote(req)  # type: ignore[attr-defined]

        quote = resp.get(ticker) if isinstance(resp, dict) else getattr(resp, ticker, None)
        if quote is None:
            return {"bid": 0.0, "ask": 0.0, "last": 0.0, "ts": datetime.now(timezone.utc)}

        bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
        ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
        last = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        ts = getattr(quote, "timestamp", datetime.now(timezone.utc))
        return {"bid": bid, "ask": ask, "last": last, "ts": ts}

    def get_historical_ohlcv(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
        end_dt = datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc)

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
            feed="iex",
        )
        bars_response = self._stock_client.get_stock_bars(request)  # type: ignore[arg-type]

        try:
            df = bars_response.df.loc[ticker]
        except KeyError:
            raise RuntimeError(f"No historical OHLCV data for {ticker}")

        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df.sort_index()

    def get_multi_ohlcv(
        self,
        tickers: list[str],
        bars: int,
        timeframe: str = "1Day",
    ) -> dict[str, pd.DataFrame]:
        """Batch fetch OHLCV for multiple tickers in one API call."""
        end = datetime.now(timezone.utc)
        lookback_days = bars * 2 if timeframe == "1Day" else max(bars // 6, 5)
        start = end - timedelta(days=lookback_days)
        tf = _TIMEFRAME_MAP.get(timeframe, TimeFrame.Day)

        request = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=tf,
            start=start,
            end=end,
            feed="iex",
        )
        bars_response = self._stock_client.get_stock_bars(request)  # type: ignore[arg-type]
        result: dict[str, pd.DataFrame] = {}

        for ticker in tickers:
            try:
                levels = bars_response.df.index.get_level_values(0)
                if ticker not in levels:
                    result[ticker] = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
                    continue
                df = bars_response.df.loc[ticker]
                df = df[["open", "high", "low", "close", "volume"]].astype(float)
                result[ticker] = df.iloc[-bars:]
            except (KeyError, AttributeError):
                result[ticker] = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        logger.info("multi_ohlcv_fetched", tickers=tickers, bars=bars, timeframe=timeframe)
        return result

    def get_options_chain(self, ticker: str, dte_min: int, dte_max: int) -> pd.DataFrame:
        today = dt.date.today()
        expiry_start = today + dt.timedelta(days=dte_min)
        expiry_end = today + dt.timedelta(days=dte_max)

        request = OptionChainRequest(
            underlying_symbol=ticker,
            expiration_date_gte=expiry_start,
            expiration_date_lte=expiry_end,
        )
        try:
            chain = self._option_client.get_option_chain(request)
        except Exception as exc:
            raise RuntimeError(f"Options chain fetch failed for {ticker}: {exc}") from exc

        if not chain:
            return pd.DataFrame()

        records = []
        for symbol, snapshot in chain.items():
            try:
                suffix = symbol[len(ticker):]
                expiry_str = suffix[:6]
                opt_type = "call" if suffix[6] == "C" else "put"
                strike = int(suffix[7:]) / 1000.0
                expiry = dt.date(2000 + int(expiry_str[:2]), int(expiry_str[2:4]), int(expiry_str[4:6]))
            except (ValueError, IndexError):
                continue

            greeks = snapshot.greeks
            delta = greeks.delta if greeks and greeks.delta is not None else None
            iv = snapshot.implied_volatility

            if delta is None or iv is None:
                continue

            bid = snapshot.latest_quote.bid_price if snapshot.latest_quote else 0.0
            ask = snapshot.latest_quote.ask_price if snapshot.latest_quote else 0.0

            records.append({
                "symbol": symbol,
                "strike": strike,
                "expiry": expiry,
                "option_type": opt_type,
                "delta": float(delta),
                "iv": float(iv),
                "bid": float(bid) if bid is not None else 0.0,
                "ask": float(ask) if ask is not None else 0.0,
                "volume": None,
            })

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        today_ts = pd.Timestamp(today)
        df["dte"] = (pd.to_datetime(df["expiry"]) - today_ts).dt.days
        result = df.reset_index(drop=True)
        logger.info("options_chain_fetched", ticker=ticker, contracts=len(result))
        return result

    def get_option_quote(self, occ_symbol: str) -> dict[str, Any]:
        """Return latest bid/ask for an option contract by OCC symbol."""
        if not _HAS_OPTION_QUOTE:
            logger.warning("get_option_quote_unavailable", reason="OptionLatestQuoteRequest not in SDK")
            return {"bid": 0.0, "ask": 0.0, "iv": None, "delta": None}

        req = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
        resp = self._option_client.get_option_latest_quote(req)  # type: ignore[attr-defined]

        quote = resp.get(occ_symbol) if isinstance(resp, dict) else getattr(resp, occ_symbol, None)
        if quote is None:
            return {"bid": 0.0, "ask": 0.0, "iv": None, "delta": None}

        bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
        ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
        return {"bid": bid, "ask": ask, "iv": None, "delta": None}
