import os
import structlog
import datetime as dt
from datetime import date, datetime, timezone, timedelta

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionChainRequest
from alpaca.data.timeframe import TimeFrame

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
    Requires ALPACA_API_KEY, ALPACA_SECRET_KEY in environment.
    """

    def __init__(self):
        api_key = _require_env("ALPACA_API_KEY")
        secret_key = _require_env("ALPACA_SECRET_KEY")

        self._stock_client = StockHistoricalDataClient(api_key, secret_key)
        self._option_client = OptionHistoricalDataClient(api_key, secret_key)

    def get_ohlcv(self, ticker: str, bars: int) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=bars * 2)

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
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
        logger.info("ohlcv_fetched", ticker=ticker, bars=len(result))
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
            # Parse OCC symbol: {underlying}{YYMMDD}{C|P}{strike*1000, 8 digits}
            # e.g. NVDA260626C00420000
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

            # Skip illiquid contracts with no Greeks or no IV — unusable for signal
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
