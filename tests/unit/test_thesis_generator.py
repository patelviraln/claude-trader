"""Unit tests for src/thesis_generator.py — mocks the OpenRouter HTTP call."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.signal_output import Leg, SignalCard
from src.thesis_generator import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(signal_type="SELL_PUT", iv_rank=65.0) -> SignalCard:
    leg = Leg(
        asset_class="option",
        symbol="NVDA",
        side="sell",
        qty=1,
        order_type="limit",
        limit_price=7.50,
        strike=840.0,
        expiry="2026-06-26",
        option_type="put",
        delta_estimate=-0.29,
    )
    return SignalCard(
        strategy_name="wheel",
        ticker="NVDA",
        signal_type=signal_type,
        underlying_price=884.50,
        legs=[leg],
        indicators={
            "ema50": 860.0,
            "ema50_slope": 0.45,
            "rsi14": 52.0,
            "bb_percent_b": 0.55,
            "volume_node_nearest": 850.0,
            "volume_node_type": "support",
        },
        payload={"dte": 41, "iv_rank": iv_rank},
        confidence_tier="HIGH",
        confidence_rationale="All filters passed",
    )


def _make_response(text: str) -> MagicMock:
    """Build a mock requests.Response with an OpenRouter-shaped body."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


ENV = {"OPENROUTER_API_KEY": "sk-or-test"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGenerateThesis:
    def test_returns_empty_string_when_no_api_key(self, tmp_path):
        with patch.dict("os.environ", {}, clear=True):
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)
        assert result == ""

    def test_returns_thesis_text(self, tmp_path):
        expected = "NVDA shows strong uptrend with elevated IV rank."
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post", return_value=_make_response(expected)) as mock_post:
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == expected
        assert mock_post.call_count == 1

    def test_uses_default_model_when_env_unset(self, tmp_path):
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post", return_value=_make_response("thesis")) as mock_post:
            from src.thesis_generator import generate_thesis
            generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert mock_post.call_args.kwargs["json"]["model"] == DEFAULT_MODEL

    def test_uses_scanner_model_env_override(self, tmp_path):
        env = dict(ENV, SCANNER_MODEL="anthropic/claude-sonnet-4.5")
        with patch.dict("os.environ", env, clear=True), \
             patch("src.thesis_generator.requests.post", return_value=_make_response("thesis")) as mock_post:
            from src.thesis_generator import generate_thesis
            generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert mock_post.call_args.kwargs["json"]["model"] == "anthropic/claude-sonnet-4.5"

    def test_falls_back_to_secondary_model_on_error(self, tmp_path):
        expected = "Fallback thesis."
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post",
                   side_effect=[RuntimeError("rate limited"), _make_response(expected)]) as mock_post:
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == expected
        assert mock_post.call_count == 2
        assert mock_post.call_args_list[0].kwargs["json"]["model"] == DEFAULT_MODEL
        assert mock_post.call_args_list[1].kwargs["json"]["model"] == DEFAULT_FALLBACK_MODEL

    def test_fallback_disabled_by_empty_string(self, tmp_path):
        env = dict(ENV, SCANNER_FALLBACK_MODEL="")
        with patch.dict("os.environ", env, clear=True), \
             patch("src.thesis_generator.requests.post",
                   side_effect=RuntimeError("down")) as mock_post:
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == ""
        assert mock_post.call_count == 1  # no second attempt

    def test_returns_empty_when_all_models_fail(self, tmp_path):
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post",
                   side_effect=RuntimeError("down")) as mock_post:
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == ""
        assert mock_post.call_count == 2  # primary + fallback

    def test_falls_back_on_error_body_with_200(self, tmp_path):
        """OpenRouter can return HTTP 200 with an error payload."""
        err_resp = MagicMock()
        err_resp.raise_for_status.return_value = None
        err_resp.json.return_value = {"error": {"message": "model overloaded"}}
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post",
                   side_effect=[err_resp, _make_response("ok")]) as mock_post:
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == "ok"
        assert mock_post.call_count == 2

    def test_sends_bearer_key_and_system_prompt(self, tmp_path):
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post", return_value=_make_response("thesis")) as mock_post:
            from src.thesis_generator import generate_thesis
            generate_thesis(_make_card(), iv_history_dir=tmp_path)

        kwargs = mock_post.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer sk-or-test"
        messages = kwargs["json"]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "NVDA" in messages[1]["content"]

    def test_inlines_iv_history_in_prompt(self, tmp_path):
        (tmp_path / "NVDA.jsonl").write_text(
            '{"date": "2026-05-15", "iv": 0.42}\n{"date": "2026-05-16", "iv": 0.45}\n'
        )
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post", return_value=_make_response("thesis")) as mock_post:
            from src.thesis_generator import generate_thesis
            generate_thesis(_make_card(), iv_history_dir=tmp_path)

        user_msg = mock_post.call_args.kwargs["json"]["messages"][1]["content"]
        assert "recent_iv_series" in user_msg
        assert "0.42" in user_msg and "0.45" in user_msg

    def test_retries_same_model_on_429(self, tmp_path):
        import requests as _requests
        err = _requests.HTTPError("429 Client Error")
        err.response = MagicMock(status_code=429)
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.time.sleep") as mock_sleep, \
             patch("src.thesis_generator.requests.post",
                   side_effect=[err, _make_response("recovered")]) as mock_post:
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == "recovered"
        assert mock_post.call_count == 2
        # both attempts hit the SAME (primary) model
        models = [c.kwargs["json"]["model"] for c in mock_post.call_args_list]
        assert models == [DEFAULT_MODEL, DEFAULT_MODEL]
        mock_sleep.assert_called_once()

    def test_strips_wrapping_quotes(self, tmp_path):
        with patch.dict("os.environ", ENV, clear=True), \
             patch("src.thesis_generator.requests.post",
                   return_value=_make_response('"Quoted thesis."')):
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == "Quoted thesis."
