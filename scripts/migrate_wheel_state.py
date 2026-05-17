"""
One-shot migration: move wheel_state.json into state/wheel.json.

Before (old layout):
    wheel_state.json  →  {"NVDA": {"phase": "A", ...}, "AAPL": {...}}

After (new layout):
    state/wheel.json  →  {"NVDA": {"phase": "A", ...}, "AAPL": {...}}

Usage:
    python scripts/migrate_wheel_state.py
    python scripts/migrate_wheel_state.py --source wheel_state.json --dest state/
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def migrate(source: Path, dest_dir: Path) -> None:
    if not source.exists():
        print(f"Source not found: {source} — nothing to migrate.")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "wheel.json"

    content = source.read_text(encoding="utf-8")
    state = json.loads(content)
    tickers = len(state)

    dest.write_text(content, encoding="utf-8")

    backup = source.with_suffix(".json.legacy")
    shutil.copy2(source, backup)

    print(f"Migrated {tickers} ticker(s) from {source} → {dest}")
    print(f"Original backed up to: {backup}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate wheel_state.json to state/wheel.json.")
    parser.add_argument("--source", default="wheel_state.json")
    parser.add_argument("--dest",   default="state")
    args = parser.parse_args()
    migrate(Path(args.source), Path(args.dest))


if __name__ == "__main__":
    main()
