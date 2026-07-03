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


def load_all_signals(signals_path: str | Path = "signals.jsonl") -> list[dict[str, Any]]:
    """Parse the whole signals JSONL once, in file (chronological) order.

    Single-pass loader for the dashboard: callers filter/slice the returned
    list in memory instead of re-reading the file per ticker.
    """
    p = Path(signals_path)
    if not p.exists():
        return []
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def filter_signals(
    records: list[dict[str, Any]],
    ticker: str | None = None,
    n: int = 50,
) -> list[dict[str, Any]]:
    """Newest-first slice of pre-loaded signal records, optionally by ticker."""
    if ticker is not None:
        t = ticker.upper()
        records = [r for r in records if r.get("ticker") == t]
    return list(reversed(records))[:n]


def get_recent_signals(
    signals_path: str | Path = "signals.jsonl",
    ticker: str | None = None,
    n: int = 50,
) -> list[dict[str, Any]]:
    """Return the last n signal records (newest first), optionally filtered by ticker."""
    return filter_signals(load_all_signals(signals_path), ticker=ticker, n=n)


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


_FIXTURE_ONLY_TICKERS = ("WXYZ", "CALLX", "EMAFAIL", "RSIFAIL", "LOWCONF")


def last_signal_by_ticker(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map ticker → most recent signal record, from pre-loaded chronological records."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in records:  # chronological; later records overwrite earlier
        if t := rec.get("ticker"):
            latest[t] = rec
    return latest


def tracked_tickers(
    states: dict[str, dict],
    records: list[dict[str, Any]],
) -> list[str]:
    """Union of tickers in pre-loaded state + signal records, fixture-only excluded."""
    tickers = set(states.keys())
    for rec in records[-500:]:
        if t := rec.get("ticker"):
            tickers.add(t)
    real = {t for t in tickers if t not in _FIXTURE_ONLY_TICKERS}
    return sorted(real) if real else sorted(tickers)


def get_all_tracked_tickers(
    state_path: str | Path = "wheel_state.json",
    signals_path: str | Path = "signals.jsonl",
) -> list[str]:
    """Union of tickers in state file and signals file, sorted."""
    return tracked_tickers(get_all_ticker_states(state_path), load_all_signals(signals_path))
