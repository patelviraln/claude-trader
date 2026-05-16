"""
Rolling IV history store: append ATM IV samples per ticker, load series for rank computation.
Each ticker gets iv_history/{ticker}.jsonl — one JSON line per trading day the signal ran.
"""
import json
import datetime
from pathlib import Path

_DEFAULT_DIR = Path("iv_history")


def load_iv_series(ticker: str, history_dir: Path = _DEFAULT_DIR) -> list[float]:
    path = history_dir / f"{ticker}.jsonl"
    if not path.exists():
        return []
    ivs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                ivs.append(float(json.loads(line)["iv"]))
            except (KeyError, ValueError):
                pass
    return ivs


def append_iv_sample(ticker: str, atm_iv: float, history_dir: Path = _DEFAULT_DIR) -> None:
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{ticker}.jsonl"
    entry = {"date": datetime.date.today().isoformat(), "iv": round(atm_iv, 4)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
