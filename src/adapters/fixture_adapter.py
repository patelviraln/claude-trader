import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


class FixtureAdapter:
    """Loads OHLCV and options chain data from pre-saved JSON fixture files.

    Fixture directory layout:
        fixtures/{ticker}_ohlcv.json       — list of OHLCV bar dicts
        fixtures/{ticker}_options.json     — list of option contract dicts
    """

    def __init__(self, fixtures_dir: str | Path = "fixtures"):
        self._dir = Path(fixtures_dir)

    def get_ohlcv(self, ticker: str, bars: int) -> pd.DataFrame:
        path = self._dir / f"{ticker}_ohlcv.json"
        if not path.exists():
            raise FileNotFoundError(f"OHLCV fixture not found: {path}")

        records = json.loads(path.read_text(encoding="utf-8"))
        df = pd.DataFrame(records)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df.iloc[-bars:]

    def get_options_chain(self, ticker: str, dte_min: int, dte_max: int) -> pd.DataFrame:
        path = self._dir / f"{ticker}_options.json"
        if not path.exists():
            raise FileNotFoundError(f"Options fixture not found: {path}")

        records = json.loads(path.read_text(encoding="utf-8"))
        df = pd.DataFrame(records)
        df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
        df["strike"] = df["strike"].astype(float)
        df["delta"] = df["delta"].astype(float)
        df["iv"] = df["iv"].astype(float)

        import datetime
        today = datetime.date.today()
        df["dte"] = (pd.to_datetime(df["expiry"]) - pd.Timestamp(today)).dt.days
        df = df[(df["dte"] >= dte_min) & (df["dte"] <= dte_max)]
        return df.reset_index(drop=True)

    def get_historical_ohlcv(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        """Return synthetic OHLCV for backtesting. Seeded by ticker name for reproducibility."""
        seed = abs(hash(ticker)) % (2 ** 31)
        rng = np.random.default_rng(seed=seed)

        dates = pd.bdate_range(start=str(start_date), end=str(end_date), freq="B", tz="UTC")
        n = len(dates)
        if n == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        # Upward-biased random walk so EMA/RSI filters regularly pass
        start_price = {"WXYZ": 150.0, "CALLX": 200.0}.get(ticker, 100.0)
        daily_ret = rng.normal(0.0008, 0.012, n)
        closes = start_price * np.exp(np.cumsum(daily_ret))

        noise = rng.uniform(0.997, 1.003, n)
        opens = closes * noise
        highs = closes * rng.uniform(1.002, 1.015, n)
        lows = closes * rng.uniform(0.985, 0.998, n)
        volumes = rng.integers(800_000, 6_000_000, n).astype(float)

        df = pd.DataFrame({
            "open":   np.round(opens, 2),
            "high":   np.round(highs, 2),
            "low":    np.round(lows, 2),
            "close":  np.round(closes, 2),
            "volume": volumes,
        }, index=dates)
        df.index.name = "timestamp"
        return df
