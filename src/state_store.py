import json
from pathlib import Path
from typing import Any


def read_state(path: str | Path) -> dict[str, Any]:
    """Read a JSON state file; return empty dict if file does not exist."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def write_state(path: str | Path, state: dict[str, Any]) -> None:
    """Atomically write state dict to JSON file (pretty-printed)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def get_ticker_state(path: str | Path, ticker: str) -> dict[str, Any]:
    """Return state for a single ticker; empty dict if ticker not present."""
    return read_state(path).get(ticker, {})


def set_ticker_state(path: str | Path, ticker: str, ticker_state: dict[str, Any]) -> None:
    """Upsert state for a single ticker without overwriting other tickers."""
    state = read_state(path)
    state[ticker] = ticker_state
    write_state(path, state)
