import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Low-level helpers (used by both old and new API)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Old API — single flat file keyed by ticker (e.g. wheel_state.json)
# Still used by signal_engine, phase_transition, and integration tests.
# ---------------------------------------------------------------------------

def get_ticker_state(path: str | Path, ticker: str) -> dict[str, Any]:
    """Return state for a single ticker; empty dict if ticker not present."""
    return read_state(path).get(ticker, {})


def set_ticker_state(path: str | Path, ticker: str, ticker_state: dict[str, Any]) -> None:
    """Upsert state for a single ticker without overwriting other tickers."""
    state = read_state(path)
    state[ticker] = ticker_state
    write_state(path, state)


# ---------------------------------------------------------------------------
# New per-strategy API (Phase A3)
# Layout: state/{strategy_name}.json  →  {ticker: {...}, ...}
# Used by the router (A5) and new strategy implementations (B1, B2, ...).
# ---------------------------------------------------------------------------

def _strategy_state_path(strategy_name: str, base_path: Path) -> Path:
    return base_path / f"{strategy_name}.json"


def get_strategy_state(
    strategy_name: str,
    ticker: str,
    base_path: Path = Path("state"),
) -> dict[str, Any]:
    """Read per-ticker state for the given strategy.

    Returns {} when no state file exists or the ticker is not present.
    """
    path = _strategy_state_path(strategy_name, base_path)
    return read_state(path).get(ticker, {})


def set_strategy_state(
    strategy_name: str,
    ticker: str,
    ticker_state: dict[str, Any],
    base_path: Path = Path("state"),
) -> None:
    """Upsert per-ticker state for the given strategy (atomic write)."""
    path = _strategy_state_path(strategy_name, base_path)
    state = read_state(path)
    state[ticker] = ticker_state
    write_state(path, state)


def all_tickers(
    strategy_name: str,
    base_path: Path = Path("state"),
) -> list[str]:
    """Return sorted list of all tickers known to a given strategy."""
    path = _strategy_state_path(strategy_name, base_path)
    return sorted(read_state(path).keys())
