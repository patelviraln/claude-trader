import datetime
import json
from pathlib import Path

import pandas as pd

# Pinned to the date fixtures were generated — keeps DTE calculations deterministic.
FIXTURE_REFERENCE_DATE = datetime.date(2026, 5, 16)


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

        # Filter to requested DTE window
        df["dte"] = (pd.to_datetime(df["expiry"]) - pd.Timestamp(FIXTURE_REFERENCE_DATE)).dt.days
        df = df[(df["dte"] >= dte_min) & (df["dte"] <= dte_max)]
        return df.reset_index(drop=True)
