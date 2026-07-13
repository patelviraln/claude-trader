"""Unit tests for src/earnings.py — yfinance lookup with local cache."""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

from src.earnings import next_earnings_date

TODAY = date(2026, 7, 13)
FUTURE = date(2026, 7, 30)


def _cache_file(tmp_path):
    return tmp_path / "earnings_cache.json"


def _write_cache(tmp_path, ticker, earnings_date, fetched):
    p = _cache_file(tmp_path)
    p.write_text(json.dumps({
        ticker: {
            "earnings_date": earnings_date.isoformat() if earnings_date else None,
            "fetched": fetched.isoformat(),
        }
    }), encoding="utf-8")
    return p


class TestNextEarningsDate:
    def test_fetches_and_caches(self, tmp_path):
        p = _cache_file(tmp_path)
        with patch("src.earnings._fetch_from_yfinance", return_value=FUTURE) as m:
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result == FUTURE
        m.assert_called_once_with("AMD")
        cached = json.loads(p.read_text(encoding="utf-8"))
        assert cached["AMD"]["earnings_date"] == "2026-07-30"

    def test_cache_hit_skips_fetch(self, tmp_path):
        p = _write_cache(tmp_path, "AMD", FUTURE, TODAY)
        with patch("src.earnings._fetch_from_yfinance") as m:
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result == FUTURE
        m.assert_not_called()

    def test_cached_none_is_honoured(self, tmp_path):
        p = _write_cache(tmp_path, "AMD", None, TODAY)
        with patch("src.earnings._fetch_from_yfinance") as m:
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result is None
        m.assert_not_called()

    def test_stale_cache_refetches(self, tmp_path):
        p = _write_cache(tmp_path, "AMD", FUTURE, date(2026, 7, 1))  # 12 days old
        new_date = date(2026, 8, 5)
        with patch("src.earnings._fetch_from_yfinance", return_value=new_date) as m:
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result == new_date
        m.assert_called_once()

    def test_past_cached_date_refetches(self, tmp_path):
        # Fresh cache but the earnings date already passed → refresh
        p = _write_cache(tmp_path, "AMD", date(2026, 7, 12), TODAY)
        with patch("src.earnings._fetch_from_yfinance", return_value=FUTURE) as m:
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result == FUTURE
        m.assert_called_once()

    def test_fetch_failure_returns_none_and_caches(self, tmp_path):
        p = _cache_file(tmp_path)
        with patch("src.earnings._fetch_from_yfinance", side_effect=RuntimeError("net down")):
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result is None
        cached = json.loads(p.read_text(encoding="utf-8"))
        assert cached["AMD"]["earnings_date"] is None

    def test_corrupt_cache_file_recovers(self, tmp_path):
        p = _cache_file(tmp_path)
        p.write_text("not-json", encoding="utf-8")
        with patch("src.earnings._fetch_from_yfinance", return_value=FUTURE):
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result == FUTURE

    def test_malformed_cache_entry_refetches(self, tmp_path):
        p = _cache_file(tmp_path)
        p.write_text(json.dumps({"AMD": {"bogus": True}}), encoding="utf-8")
        with patch("src.earnings._fetch_from_yfinance", return_value=FUTURE) as m:
            result = next_earnings_date("AMD", cache_path=p, today=TODAY)
        assert result == FUTURE
        m.assert_called_once()
