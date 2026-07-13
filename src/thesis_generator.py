"""
AI-generated trade thesis via OpenRouter (OpenAI-compatible chat completions).

Generates a 1-2 sentence plain-English rationale for a strategy signal.
The rolling IV history for the ticker is inlined into the prompt so any
model can use it — no tool-calling support required.

Environment (.env):
    OPENROUTER_API_KEY      — required; get one at https://openrouter.ai/keys
    SCANNER_MODEL           — optional primary model (default: openai/gpt-oss-120b:free)
    SCANNER_FALLBACK_MODEL  — optional; tried automatically if the primary model
                              errors. Set to "" to disable the fallback.

Gracefully degrades (returns "") if OPENROUTER_API_KEY is unset or every
model attempt fails.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
import structlog

import src.logger as _log_setup  # noqa: F401

if TYPE_CHECKING:
    from src.signal_output import SignalCard

logger = structlog.get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b:free"
DEFAULT_FALLBACK_MODEL = "google/gemini-2.5-flash"
REQUEST_TIMEOUT_S = 60
RATE_LIMIT_RETRIES = 2      # extra attempts per model on HTTP 429
RATE_LIMIT_BACKOFF_S = 5.0  # wait between 429 retries (free models are heavily limited)

_SYSTEM_PROMPT = """\
You are a professional trader writing concise trade thesis statements for a
multi-strategy signal system.

Strategies you may see:
- wheel: Phase A (SELL_PUT) sells cash-secured puts on bullish underlyings;
  Phase B (SELL_CALL) sells covered calls against 100 shares held.
- put_credit_spread (SELL_PUT_SPREAD): sells a ~0.30-delta put and buys a
  ~0.15-delta put at the same expiry for a defined-risk net credit.
- rsi2 (BUY_EQUITY / SELL_EQUITY): Connors RSI(2) mean-reversion — buys
  oversold dips above the 200-day SMA, exits on RSI recovery or max hold.
- red_day_csp (SELL_PUT): sells ~0.22-delta cash-secured puts on red days
  where the dip is sentiment/market drag (news attribution found no
  fundamental break, no earnings inside the window, IV rank elevated).

Signals only fire after the strategy's full technical filter chain passes
(trend, momentum, volatility, and liquidity gates as applicable).

Your task: write ONE sentence (<= 25 words) of plain-English thesis that
explains WHY this specific trade makes sense right now, referencing key
indicator values provided. Be concrete, not generic. Do not mention filter
names directly. Output ONLY the thesis sentence — no preamble, no quotes."""


def _build_snapshot(card: "SignalCard", iv_history_dir: Path) -> dict[str, Any]:
    """Market snapshot for the user message, including recent IV history."""
    leg = card.legs[0] if card.legs else None
    snapshot: dict[str, Any] = {
        "strategy": card.strategy_name,
        "ticker": card.ticker,
        "signal_type": card.signal_type,
        "underlying_price": card.underlying_price,
        "strike": leg.strike if leg else None,
        "expiry": leg.expiry if leg else None,
        "qty": leg.qty if leg else None,
        "delta_estimate": leg.delta_estimate if leg else None,
        "limit_price": leg.limit_price if leg else None,
        "dte": card.payload.get("dte"),
        "iv_rank": card.payload.get("iv_rank"),
        "confidence_tier": card.confidence_tier,
        "risk_flags": card.risk_flags,
    }
    snapshot.update(card.indicators)

    try:
        from src.iv_history import load_iv_series
        series = load_iv_series(card.ticker, iv_history_dir)
        if series:
            snapshot["recent_iv_series"] = [round(v, 4) for v in series[-30:]]
    except Exception:
        pass

    return snapshot


def _call_openrouter(api_key: str, model: str, snapshot: dict[str, Any]) -> str:
    """Single chat-completion call; returns the thesis text or raises."""
    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Optional attribution headers recommended by OpenRouter
            "HTTP-Referer": "https://github.com/patelviraln/claude-trader",
            "X-Title": "claude-trader",
        },
        json={
            "model": model,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Generate a trade thesis for this signal:\n\n"
                        f"{json.dumps(snapshot, indent=2)}"
                    ),
                },
            ],
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:  # OpenRouter can return 200 with an error body
        raise RuntimeError(f"OpenRouter error: {data['error']}")
    text = data["choices"][0]["message"]["content"] or ""
    return text.strip().strip('"')


def generate_thesis(
    card: "SignalCard",
    iv_history_dir: Path = Path("iv_history"),
) -> str:
    """Return a 1-2 sentence thesis string, or "" on any failure."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return ""

    primary = os.environ.get("SCANNER_MODEL") or DEFAULT_MODEL
    fallback_env = os.environ.get("SCANNER_FALLBACK_MODEL")
    fallback = DEFAULT_FALLBACK_MODEL if fallback_env is None else fallback_env

    snapshot = _build_snapshot(card, iv_history_dir)

    models = [primary] + ([fallback] if fallback and fallback != primary else [])
    for model in models:
        for attempt in range(1 + RATE_LIMIT_RETRIES):
            try:
                text = _call_openrouter(api_key, model, snapshot)
                if text:
                    return text
                logger.warning("thesis.empty_response", ticker=card.ticker, model=model)
                break  # empty but successful — retrying won't help; try next model
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 429 and attempt < RATE_LIMIT_RETRIES:
                    logger.warning("thesis.rate_limited", ticker=card.ticker, model=model,
                                   retry_in_s=RATE_LIMIT_BACKOFF_S)
                    time.sleep(RATE_LIMIT_BACKOFF_S)
                    continue
                logger.warning("thesis.model_failed", ticker=card.ticker, model=model, error=str(exc))
                break
            except Exception as exc:
                logger.warning("thesis.model_failed", ticker=card.ticker, model=model, error=str(exc))
                break

    return ""
