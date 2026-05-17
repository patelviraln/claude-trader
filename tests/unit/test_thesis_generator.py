"""Unit tests for src/thesis_generator.py — mocks the Anthropic client."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.signal_output import Leg, SignalCard


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


def _make_end_turn_response(text: str) -> MagicMock:
    """Build a mock Anthropic response that ends on end_turn."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    return resp


def _make_tool_use_response(tool_id: str, tool_name: str, tool_input: dict) -> MagicMock:
    """Build a mock Anthropic response that triggers a tool call."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = tool_name
    block.input = tool_input
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGenerateThesis:
    def test_returns_empty_string_when_no_api_key(self, tmp_path):
        with patch.dict("os.environ", {}, clear=True):
            from src.thesis_generator import generate_thesis
            result = generate_thesis(_make_card(), iv_history_dir=tmp_path)
        assert result == ""

    def test_returns_empty_string_when_anthropic_not_installed(self, tmp_path):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("src.thesis_generator.anthropic", None):
                from src.thesis_generator import generate_thesis
                result = generate_thesis(_make_card(), iv_history_dir=tmp_path)
        assert result == ""

    def test_returns_thesis_text_on_end_turn(self, tmp_path):
        expected = "NVDA shows strong uptrend with elevated IV rank."
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("src.thesis_generator.anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_anthropic.Anthropic.return_value = mock_client
                mock_client.messages.create.return_value = _make_end_turn_response(expected)

                from src.thesis_generator import generate_thesis
                result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == expected

    def test_calls_tool_and_continues(self, tmp_path):
        (tmp_path / "NVDA.jsonl").write_text(
            '{"date": "2026-05-15", "iv": 0.42}\n{"date": "2026-05-16", "iv": 0.45}\n'
        )
        final_text = "NVDA IV trending up; selling put at support near volume node."

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("src.thesis_generator.anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_anthropic.Anthropic.return_value = mock_client

                tool_response = _make_tool_use_response(
                    "toolu_1", "fetch_iv_history", {"ticker": "NVDA"}
                )
                end_response = _make_end_turn_response(final_text)
                mock_client.messages.create.side_effect = [tool_response, end_response]

                from src.thesis_generator import generate_thesis
                result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == final_text
        assert mock_client.messages.create.call_count == 2

    def test_returns_empty_on_api_exception(self, tmp_path):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("src.thesis_generator.anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_anthropic.Anthropic.return_value = mock_client
                mock_client.messages.create.side_effect = RuntimeError("API down")

                from src.thesis_generator import generate_thesis
                result = generate_thesis(_make_card(), iv_history_dir=tmp_path)

        assert result == ""

    def test_passes_cached_system_prompt(self, tmp_path):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("src.thesis_generator.anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_anthropic.Anthropic.return_value = mock_client
                mock_client.messages.create.return_value = _make_end_turn_response("thesis")

                from src.thesis_generator import generate_thesis
                generate_thesis(_make_card(), iv_history_dir=tmp_path)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        system = call_kwargs.get("system", [])
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_uses_opus_4_7_model(self, tmp_path):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("src.thesis_generator.anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_anthropic.Anthropic.return_value = mock_client
                mock_client.messages.create.return_value = _make_end_turn_response("thesis")

                from src.thesis_generator import generate_thesis
                generate_thesis(_make_card(), iv_history_dir=tmp_path)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-opus-4-7"

    def test_uses_adaptive_thinking(self, tmp_path):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("src.thesis_generator.anthropic") as mock_anthropic:
                mock_client = MagicMock()
                mock_anthropic.Anthropic.return_value = mock_client
                mock_client.messages.create.return_value = _make_end_turn_response("thesis")

                from src.thesis_generator import generate_thesis
                generate_thesis(_make_card(), iv_history_dir=tmp_path)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs.get("thinking") == {"type": "adaptive"}
