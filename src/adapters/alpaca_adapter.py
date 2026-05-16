import os
from datetime import datetime, timezone, timedelta

import pandas as pd


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

        api_key = os.environ["ALPACA_API_KEY"]
        secret_key = os.environ["ALPACA_SECRET_KEY"]

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
            feed="iex",
        )
        bars_response = self._stock_client.get_stock_bars(request)
        df = bars_response.df.loc[ticker] if ticker in bars_response.df.index.get_level_values(0) else pd.DataFrame()
        df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df.iloc[-bars:]

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
            # Parse OCC symbol: e.g. NVDA260626C00420000
            # Format: {underlying}{YYMMDD}{C|P}{strike*1000 zero-padded to 8 digits}
            try:
                suffix = symbol[len(ticker):]          # "260626C00420000"
                expiry_str = suffix[:6]                # "260626"
                opt_type = "call" if suffix[6] == "C" else "put"
                strike = int(suffix[7:]) / 1000.0
                expiry = dt.date(2000 + int(expiry_str[:2]), int(expiry_str[2:4]), int(expiry_str[4:6]))
            except (ValueError, IndexError):
                continue
            greeks = snapshot.greeks
            records.append({
                "symbol": symbol,
                "strike": strike,
                "expiry": expiry,
                "option_type": opt_type,
                "delta": greeks.delta if greeks else None,
                "iv": snapshot.implied_volatility,
                "bid": snapshot.latest_quote.bid_price if snapshot.latest_quote else None,
                "ask": snapshot.latest_quote.ask_price if snapshot.latest_quote else None,
                "volume": None,
            })

        df = pd.DataFrame(records)
        today_ts = pd.Timestamp(today)
        df["dte"] = (pd.to_datetime(df["expiry"]) - today_ts).dt.days
        return df.reset_index(drop=True)
