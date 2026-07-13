"""
Dip attribution: classify WHY a stock is down today.

Ported from the red-day-sentiment-scanner skill's Attribution Analyst agent.
Headlines come from Alpaca's free News API; classification is done by an
OpenRouter model using the same primary/fallback/429-retry policy as
src/thesis_generator.py (SCANNER_MODEL -> SCANNER_FALLBACK_MODEL).

Categories:
    FUNDAMENTAL_BREAK — thesis-changing negative (guidance cut, regulatory
                        action, dilution, short report, ...)  → hard reject
    TRANSIENT         — sentiment/sector/macro noise           → candidate
    EVENT_PENDING     — known binary ahead (earnings, FDA, court) → reject
    NO_NEWS           — nothing discoverable in the last 48h   → caution
    UNAVAILABLE       — classification impossible (no API key, all models
                        failed, malformed response)            → conservative

Results are cached per ticker per day in state/attribution_cache.json so
dashboard reruns and repeated scans don't re-bill the LLM.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
from src.thesis_generator import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_MODEL,
    OPENROUTER_URL,
    RATE_LIMIT_BACKOFF_S,
    RATE_LIMIT_RETRIES,
    REQUEST_TIMEOUT_S,
)

logger = structlog.get_logger(__name__)

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
NEWS_LOOKBACK_HOURS = 48
NEWS_LIMIT = 20
CACHE_PATH = Path("state/attribution_cache.json")

CATEGORIES = ("FUNDAMENTAL_BREAK", "TRANSIENT", "EVENT_PENDING", "NO_NEWS")


class Attribution(BaseModel):
    category: str  # one of CATEGORIES or "UNAVAILABLE"
    reason: str


_SYSTEM_PROMPT = """\
You are the Attribution Analyst on a systematic options desk. A watchlist
stock is down today and you must classify WHY, using ONLY the headlines
provided. Never use prior knowledge of the company — if the headlines don't
support a category, that is NO_NEWS.

Classify into exactly ONE category:

FUNDAMENTAL_BREAK — thesis-changing negative: guidance cut or withdrawn
(any magnitude), pre-announced miss, lost major customer/contract,
regulatory/DOJ/FTC/SEC action, accounting irregularity or delayed filing,
secondary offering / dilution, credit downgrade of the company's debt,
analyst downgrade WITH thesis-change ("structural") language, published
short-seller report (always FUNDAMENTAL_BREAK regardless of merit),
key executive exit under adverse circumstances, product failure/recall/
clinical failure, loss of index inclusion, export-control news for
directly-exposed names.

TRANSIENT — noise/sentiment: market-wide risk-off, sector sympathy with a
peer's bad news (only if the peer's problem is company-specific, not
"the whole end-market is weakening"), macro headlines with no company
linkage, analyst price-target trim with rating UNCHANGED, profit-taking
after a large run-up, options-expiry pinning, fund rebalancing/index flows,
generic "AI trade unwinding" pieces without company-specific content.

EVENT_PENDING — a known binary event lies ahead: earnings within ~14 days,
FDA decision, court ruling, contract award date, major product event,
lockup expiry.

NO_NEWS — the headlines contain nothing that explains the move.

Tie-break rules: fundamental beats transient, always. A valuation-only
downgrade ("great company, rich price") is TRANSIENT; a rating cut with
structural language is FUNDAMENTAL_BREAK.

Respond with ONLY a JSON object, no markdown fences, no prose:
{"category": "<one of FUNDAMENTAL_BREAK|TRANSIENT|EVENT_PENDING|NO_NEWS>",
 "reason": "<one sentence citing the specific headline(s) that decided it>"}"""


# ---------------------------------------------------------------------------
# News fetch (Alpaca News API — free tier, same keys as trading)
# ---------------------------------------------------------------------------

def fetch_news(ticker: str, hours: int = NEWS_LOOKBACK_HOURS) -> list[dict[str, Any]]:
    """Return recent headlines: [{"headline", "summary", "source", "created_at"}].

    Raises on HTTP/network errors so callers can distinguish "no news" from
    "couldn't check".
    """
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")

    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    response = requests.get(
        NEWS_URL,
        headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
        params={"symbols": ticker, "start": start, "limit": NEWS_LIMIT,
                "sort": "desc"},
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    articles = response.json().get("news", [])
    return [
        {
            "headline": a.get("headline", ""),
            "summary": (a.get("summary") or "")[:300],
            "source": a.get("source", ""),
            "created_at": a.get("created_at", ""),
        }
        for a in articles
    ]


# ---------------------------------------------------------------------------
# LLM classification (OpenRouter, thesis_generator model policy)
# ---------------------------------------------------------------------------

def _call_classifier(api_key: str, model: str, user_content: str) -> str:
    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/patelviraln/claude-trader",
            "X-Title": "claude-trader",
        },
        json={
            "model": model,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"OpenRouter error: {data['error']}")
    return data["choices"][0]["message"]["content"] or ""


def _parse_classification(text: str) -> Attribution | None:
    """Parse the model's JSON reply; None when unusable."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    category = str(obj.get("category", "")).strip().upper()
    if category not in CATEGORIES:
        return None
    return Attribution(category=category,
                       reason=str(obj.get("reason", ""))[:300])


def _classify_with_llm(
    ticker: str,
    day_move_pct: float,
    residual_tag: str,
    headlines: list[dict[str, Any]],
) -> Attribution:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return Attribution(category="UNAVAILABLE",
                           reason="OPENROUTER_API_KEY not set")

    primary = os.environ.get("SCANNER_MODEL") or DEFAULT_MODEL
    fallback_env = os.environ.get("SCANNER_FALLBACK_MODEL")
    fallback = DEFAULT_FALLBACK_MODEL if fallback_env is None else fallback_env

    lines = [
        f"- [{h['source']}] {h['headline']}" + (f" — {h['summary']}" if h["summary"] else "")
        for h in headlines
    ]
    user_content = (
        f"Ticker: {ticker}\n"
        f"Day move: {day_move_pct:+.2f}%\n"
        f"Beta-residual tag: {residual_tag} "
        f"(SYMPATHY = market explains the move; IDIOSYNCRATIC = it doesn't)\n\n"
        f"Headlines from the last {NEWS_LOOKBACK_HOURS}h:\n" + "\n".join(lines)
    )

    models = [primary] + ([fallback] if fallback and fallback != primary else [])
    for model in models:
        for attempt in range(1 + RATE_LIMIT_RETRIES):
            try:
                text = _call_classifier(api_key, model, user_content)
                parsed = _parse_classification(text)
                if parsed:
                    logger.info("attribution.classified", ticker=ticker,
                                model=model, category=parsed.category)
                    return parsed
                logger.warning("attribution.unparseable", ticker=ticker,
                               model=model, raw=text[:200])
                break  # malformed but successful — try next model
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 429 and attempt < RATE_LIMIT_RETRIES:
                    logger.warning("attribution.rate_limited", ticker=ticker,
                                   model=model, retry_in_s=RATE_LIMIT_BACKOFF_S)
                    time.sleep(RATE_LIMIT_BACKOFF_S)
                    continue
                logger.warning("attribution.model_failed", ticker=ticker,
                               model=model, error=str(exc))
                break
            except Exception as exc:
                logger.warning("attribution.model_failed", ticker=ticker,
                               model=model, error=str(exc))
                break

    return Attribution(category="UNAVAILABLE",
                       reason="All classification models failed")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify_dip(
    ticker: str,
    day_move_pct: float,
    residual_tag: str,
    cache_path: Path = CACHE_PATH,
    today: date | None = None,
) -> Attribution:
    """Fetch news and classify today's dip. Cached per ticker per day.

    UNAVAILABLE results are NOT cached — a later rerun may succeed.
    """
    today = today or date.today()
    key = f"{ticker}:{today.isoformat()}"

    cache = _load_cache(cache_path)
    if key in cache:
        entry = cache[key]
        return Attribution(category=entry["category"], reason=entry["reason"])

    try:
        headlines = fetch_news(ticker)
    except Exception as exc:
        logger.warning("attribution.news_failed", ticker=ticker, error=str(exc))
        return Attribution(category="UNAVAILABLE",
                           reason=f"News fetch failed: {exc}")

    if not headlines:
        result = Attribution(
            category="NO_NEWS",
            reason=f"No headlines for {ticker} in the last {NEWS_LOOKBACK_HOURS}h",
        )
    else:
        result = _classify_with_llm(ticker, day_move_pct, residual_tag, headlines)

    if result.category != "UNAVAILABLE":
        cache[key] = {"category": result.category, "reason": result.reason}
        # prune entries from previous days to keep the file small
        prefix = f":{today.isoformat()}"
        cache = {k: v for k, v in cache.items() if k.endswith(prefix)}
        try:
            _save_cache(cache, cache_path)
        except OSError as exc:
            logger.warning("attribution.cache_write_failed", error=str(exc))

    return result
