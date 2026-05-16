"""
Dashboard data layer — reads from the same flat files the CLI writes.
No database; everything is derived from wheel_state.json, signals.jsonl,
and iv_history/*.jsonl.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def get_all_ticker_states(state_path: str | Path = "wheel_state.json") -> dict[str, dict]:
    p = Path(state_path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def get_recent_signals(
    signals_path: str | Path = "signals.jsonl",
    ticker: str | None = None,
    n: int = 50,
) -> list[dict[str, Any]]:
    """Return the last n signal records (newest first), optionally filtered by ticker."""
    p = Path(signals_path)
    if not p.exists():
        return []
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if ticker is None or rec.get("ticker") == ticker.upper():
                records.append(rec)
        except json.JSONDecodeError:
            continue
    return list(reversed(records))[:n]


def get_iv_series(
    ticker: str,
    history_dir: str | Path = "iv_history",
) -> list[dict[str, Any]]:
    """Return [{date, iv}] entries for a ticker in chronological order."""
    p = Path(history_dir) / f"{ticker.upper()}.jsonl"
    if not p.exists():
        return []
    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if "date" in entry and "iv" in entry:
                entries.append({"date": entry["date"], "iv": entry["iv"]})
        except json.JSONDecodeError:
            continue
    return entries


def get_last_signal(
    ticker: str,
    signals_path: str | Path = "signals.jsonl",
) -> dict[str, Any] | None:
    """Return the most recent signal record for a ticker."""
    signals = get_recent_signals(signals_path, ticker=ticker, n=1)
    return signals[0] if signals else None


def get_all_tracked_tickers(
    state_path: str | Path = "wheel_state.json",
    signals_path: str | Path = "signals.jsonl",
) -> list[str]:
    """Union of tickers in state file and signals file, sorted."""
    tickers = set(get_all_ticker_states(state_path).keys())
    for rec in get_recent_signals(signals_path, n=500):
        if t := rec.get("ticker"):
            tickers.add(t)
    # Filter out fixture-only tickers
    real = {t for t in tickers if t not in ("WXYZ", "CALLX", "EMAFAIL", "RSIFAIL", "LOWCONF")}
    return sorted(real) if real else sorted(tickers)
