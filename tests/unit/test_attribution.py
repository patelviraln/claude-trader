"""Unit tests for src/attribution.py — news fetch + LLM dip classification."""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.attribution import (
    Attribution,
    _parse_classification,
    classify_dip,
    fetch_news,
)

TODAY = date(2026, 7, 13)

ENV = {
    "ALPACA_API_KEY": "key",
    "ALPACA_SECRET_KEY": "secret",
    "OPENROUTER_API_KEY": "or-key",
    "SCANNER_MODEL": "test/primary",
    "SCANNER_FALLBACK_MODEL": "test/fallback",
}

HEADLINES = [{"headline": "AMD falls with chip sector", "summary": "",
              "source": "benzinga", "created_at": "2026-07-13T10:00:00Z"}]


def _news_response(articles):
    resp = MagicMock()
    resp.json.return_value = {"news": articles}
    resp.raise_for_status.return_value = None
    return resp


def _llm_response(content):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status.return_value = None
    return resp


def _http_429():
    err_resp = MagicMock()
    err_resp.status_code = 429
    exc = requests.HTTPError(response=err_resp)
    resp = MagicMock()
    resp.raise_for_status.side_effect = exc
    return resp


class TestParseClassification:
    def test_clean_json(self):
        out = _parse_classification('{"category": "TRANSIENT", "reason": "sector drag"}')
        assert out.category == "TRANSIENT"
        assert out.reason == "sector drag"

    def test_fenced_json(self):
        out = _parse_classification('```json\n{"category": "NO_NEWS", "reason": "x"}\n```')
        assert out.category == "NO_NEWS"

    def test_json_embedded_in_prose(self):
        out = _parse_classification('Here: {"category": "EVENT_PENDING", "reason": "earnings"} done')
        assert out.category == "EVENT_PENDING"

    def test_invalid_category(self):
        assert _parse_classification('{"category": "MAYBE", "reason": "?"}') is None

    def test_garbage(self):
        assert _parse_classification("not json at all") is None

    def test_lowercase_category_normalised(self):
        out = _parse_classification('{"category": "transient", "reason": "x"}')
        assert out.category == "TRANSIENT"


class TestFetchNews:
    def test_missing_keys_raises(self, monkeypatch):
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError):
            fetch_news("AMD")

    def test_parses_articles(self, monkeypatch):
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)
        articles = [{"headline": "H1", "summary": "S1" * 400, "source": "src",
                     "created_at": "t"}]
        with patch("src.attribution.requests.get", return_value=_news_response(articles)):
            out = fetch_news("AMD")
        assert out[0]["headline"] == "H1"
        assert len(out[0]["summary"]) == 300  # truncated


class TestClassifyDip:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)

    def test_happy_path_classifies_and_caches(self, tmp_path):
        cache = tmp_path / "attr.json"
        llm = _llm_response('{"category": "TRANSIENT", "reason": "sector sympathy"}')
        with patch("src.attribution.fetch_news", return_value=HEADLINES), \
             patch("src.attribution.requests.post", return_value=llm):
            out = classify_dip("AMD", -3.1, "SYMPATHY", cache_path=cache, today=TODAY)
        assert out.category == "TRANSIENT"
        cached = json.loads(cache.read_text(encoding="utf-8"))
        assert cached[f"AMD:{TODAY.isoformat()}"]["category"] == "TRANSIENT"

    def test_cache_hit_skips_everything(self, tmp_path):
        cache = tmp_path / "attr.json"
        cache.write_text(json.dumps({
            f"AMD:{TODAY.isoformat()}": {"category": "NO_NEWS", "reason": "cached"}
        }), encoding="utf-8")
        with patch("src.attribution.fetch_news") as mock_news:
            out = classify_dip("AMD", -3.1, "SYMPATHY", cache_path=cache, today=TODAY)
        assert out.category == "NO_NEWS"
        assert out.reason == "cached"
        mock_news.assert_not_called()

    def test_no_headlines_is_no_news_without_llm(self, tmp_path):
        cache = tmp_path / "attr.json"
        with patch("src.attribution.fetch_news", return_value=[]), \
             patch("src.attribution.requests.post") as mock_post:
            out = classify_dip("AMD", -3.1, "MIXED", cache_path=cache, today=TODAY)
        assert out.category == "NO_NEWS"
        mock_post.assert_not_called()

    def test_news_failure_is_unavailable_and_not_cached(self, tmp_path):
        cache = tmp_path / "attr.json"
        with patch("src.attribution.fetch_news", side_effect=RuntimeError("api down")):
            out = classify_dip("AMD", -3.1, "MIXED", cache_path=cache, today=TODAY)
        assert out.category == "UNAVAILABLE"
        assert not cache.exists()

    def test_429_falls_back_to_second_model(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.attribution.RATE_LIMIT_BACKOFF_S", 0.0)
        cache = tmp_path / "attr.json"
        good = _llm_response('{"category": "FUNDAMENTAL_BREAK", "reason": "guidance cut"}')
        # primary: 429 on every attempt (1 + retries), then fallback succeeds
        with patch("src.attribution.fetch_news", return_value=HEADLINES), \
             patch("src.attribution.requests.post",
                   side_effect=[_http_429(), _http_429(), _http_429(), good]), \
             patch("src.attribution.time.sleep"):
            out = classify_dip("AMD", -3.1, "IDIOSYNCRATIC", cache_path=cache, today=TODAY)
        assert out.category == "FUNDAMENTAL_BREAK"

    def test_all_models_fail_is_unavailable(self, tmp_path):
        cache = tmp_path / "attr.json"
        bad = _llm_response("no json here")
        with patch("src.attribution.fetch_news", return_value=HEADLINES), \
             patch("src.attribution.requests.post", return_value=bad):
            out = classify_dip("AMD", -3.1, "MIXED", cache_path=cache, today=TODAY)
        assert out.category == "UNAVAILABLE"
        assert not cache.exists()  # UNAVAILABLE is never cached

    def test_no_openrouter_key_is_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        cache = tmp_path / "attr.json"
        with patch("src.attribution.fetch_news", return_value=HEADLINES):
            out = classify_dip("AMD", -3.1, "SYMPATHY", cache_path=cache, today=TODAY)
        assert out.category == "UNAVAILABLE"

    def test_old_day_entries_pruned(self, tmp_path):
        cache = tmp_path / "attr.json"
        cache.write_text(json.dumps({
            "NVDA:2026-07-10": {"category": "TRANSIENT", "reason": "old"}
        }), encoding="utf-8")
        llm = _llm_response('{"category": "TRANSIENT", "reason": "fresh"}')
        with patch("src.attribution.fetch_news", return_value=HEADLINES), \
             patch("src.attribution.requests.post", return_value=llm):
            classify_dip("AMD", -3.1, "SYMPATHY", cache_path=cache, today=TODAY)
        cached = json.loads(cache.read_text(encoding="utf-8"))
        assert "NVDA:2026-07-10" not in cached
        assert f"AMD:{TODAY.isoformat()}" in cached
