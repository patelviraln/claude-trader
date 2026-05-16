"""
AI-generated trade thesis using the Claude API.

Generates a 1-2 sentence plain-English rationale for a Wheel strategy signal.
Uses adaptive thinking, a cached system prompt, and one tool (fetch_iv_history)
so Claude can inspect rolling IV trend before writing the thesis.

Gracefully degrades (returns "") if ANTHROPIC_API_KEY is unset or the
`anthropic` package is not installed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import anthropic as anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from src.signal_output import SignalCard

_SYSTEM_PROMPT = """\
You are a professional options trader writing concise trade thesis statements for
a Wheel income strategy.

The Wheel strategy:
- Phase A (SELL_PUT): sell cash-secured puts on bullish underlyings to collect premium
  or get assigned shares at a discount.
- Phase B (SELL_CALL): sell covered calls against 100 shares already held to collect
  premium or exit the position at a profit.

Filters before a signal is issued:
1. Phase gate — correct phase for the trade
2. EMA-50 trend — price above (Phase A) or below (Phase B) the 50-day EMA
3. RSI-14 momentum — not overbought/oversold
4. Bollinger Bands — position within bands
5. Options chain — valid puts/calls exist in the 30-45 DTE window
6. Delta filter — put delta between -0.25 and -0.35 (or call delta 0.25-0.35)
7. Volume profile — strike near high-volume node for support/resistance

Your task: write ONE sentence (≤ 25 words) of plain-English thesis that explains
WHY this specific trade makes sense right now, referencing key indicator values
provided. Be concrete, not generic. Do not mention filter names directly.
Output ONLY the thesis sentence — no preamble, no quotes."""


def _extract_text(content: list[Any]) -> str:
    for block in content:
        if hasattr(block, "type") and block.type == "text":
            return block.text.strip()
    return ""


def generate_thesis(
    card: "SignalCard",
    iv_history_dir: Path = Path("iv_history"),
) -> str:
    """Return a 1-2 sentence thesis string, or "" on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""

    if anthropic is None:
        return ""

    # Build market snapshot for the user message
    ir = card.indicator_readings
    snapshot: dict[str, Any] = {
        "ticker": card.ticker,
        "phase": card.phase,
        "underlying_price": card.underlying_price,
        "recommended_strike": card.recommended_strike,
        "recommended_expiry": card.recommended_expiry,
        "dte": card.dte,
        "delta_estimate": card.delta_estimate,
        "iv_rank": card.iv_rank,
        "confidence_tier": card.confidence_tier,
        "risk_flags": card.risk_flags,
    }
    if ir:
        snapshot.update({
            "ema50": ir.ema50,
            "ema50_slope": ir.ema50_slope,
            "rsi14": ir.rsi14,
            "bb_percent_b": ir.bb_percent_b,
            "volume_node_nearest": ir.volume_node_nearest,
            "volume_node_type": ir.volume_node_type,
        })

    tools = [
        {
            "name": "fetch_iv_history",
            "description": (
                "Returns the rolling historical IV series for a ticker so you can "
                "assess whether current IV is trending up, down, or stable."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol, e.g. NVDA"}
                },
                "required": ["ticker"],
            },
        }
    ]

    def _call_tool(name: str, tool_input: dict[str, Any]) -> str:
        if name == "fetch_iv_history":
            try:
                from src.iv_history import load_iv_series
                series = load_iv_series(tool_input["ticker"], iv_history_dir)
                return json.dumps({"iv_series": series, "count": len(series)})
            except Exception as exc:
                return json.dumps({"error": str(exc)})
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        client = anthropic.Anthropic(api_key=api_key)

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Generate a trade thesis for this signal:\n\n"
                    f"{json.dumps(snapshot, indent=2)}"
                ),
            }
        ]

        # Agentic tool-use loop (at most 3 iterations to bound cost)
        for _ in range(3):
            response = client.messages.create(
                model="claude-opus-4-7",
                max_tokens=512,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                return _extract_text(response.content)

            if response.stop_reason == "tool_use":
                # Append assistant turn
                messages.append({"role": "assistant", "content": response.content})
                # Execute tool calls and build tool results
                tool_results = []
                for block in response.content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        result = _call_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            }
                        )
                messages.append({"role": "user", "content": tool_results})
                continue

            # Any other stop reason — just extract text
            return _extract_text(response.content)

        return _extract_text(response.content)

    except Exception:
        return ""
