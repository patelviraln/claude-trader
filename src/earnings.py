"""
Next-earnings-date lookup for the red_day_csp earnings gate.

Alpaca provides no earnings calendar, so this uses yfinance (free, no key).
Results are cached in state/earnings_cache.json with a 3-day TTL so the daily
pipeline makes at most one yfinance call per ticker every few days.

next_earnings_date() returns None on any failure — the EarningsGateFilter
treats an unknown date as a hard block (unverified != clear).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import structlog

import src.logger as _log_setup  # noqa: F401

logger = structlog.get_logger(__name__)

CACHE_PATH = Path("state/earnings_cache.json")
CACHE_TTL_DAYS = 3


def _load_cache(cache_path: Path) -> dict:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp.replace(cache_path)


def _fetch_from_yfinance(ticker: str) -> date | None:
    """Query yfinance for the next earnings date. Returns None on any failure."""
    import yfinance as yf

    t = yf.Ticker(ticker)

    # Primary: calendar dict — {"Earnings Date": [date, ...], ...}
    try:
        cal = t.calendar
        earnings_dates = (cal or {}).get("Earnings Date") or []
        future = sorted(d for d in earnings_dates if d >= date.today())
        if future:
            return future[0]
    except Exception as exc:
        logger.debug("earnings.calendar_failed", ticker=ticker, error=str(exc))

    # Fallback: earnings_dates DataFrame (index = timestamps, includes future rows)
    try:
        df = t.get_earnings_dates(limit=8)
        if df is not None and not df.empty:
            future_idx = sorted(
                ts.date() for ts in df.index if ts.date() >= date.today()
            )
            if future_idx:
                return future_idx[0]
    except Exception as exc:
        logger.debug("earnings.earnings_dates_failed", ticker=ticker, error=str(exc))

    return None


def next_earnings_date(
    ticker: str,
    cache_path: Path = CACHE_PATH,
    today: date | None = None,
) -> date | None:
    """Return the next known earnings date for ticker, or None if unknown.

    Cached for CACHE_TTL_DAYS; a cached "unknown" (null) is also honoured so
    repeated dashboard reruns don't hammer yfinance for dateless tickers.
    A cached date that has already passed forces a refresh.
    """
    today = today or date.today()
    cache = _load_cache(cache_path)
    entry = cache.get(ticker)

    if entry:
        try:
            fetched = datetime.fromisoformat(entry["fetched"]).date()
            fresh = (today - fetched) <= timedelta(days=CACHE_TTL_DAYS)
            cached_date = (
                date.fromisoformat(entry["earnings_date"])
                if entry.get("earnings_date")
                else None
            )
            if fresh and (cached_date is None or cached_date >= today):
                return cached_date
        except (KeyError, ValueError):
            pass  # malformed entry — refetch

    try:
        result = _fetch_from_yfinance(ticker)
    except Exception as exc:
        logger.warning("earnings.fetch_failed", ticker=ticker, error=str(exc))
        result = None

    cache[ticker] = {
        "earnings_date": result.isoformat() if result else None,
        "fetched": today.isoformat(),
    }
    try:
        _save_cache(cache, cache_path)
    except OSError as exc:
        logger.warning("earnings.cache_write_failed", error=str(exc))

    logger.info("earnings.fetched", ticker=ticker,
                earnings_date=result.isoformat() if result else None)
    return result
