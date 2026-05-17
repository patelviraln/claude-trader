"""
One-shot migration: convert signals.jsonl from old schema to new schema.

Old schema fields:
  phase, recommended_strike, recommended_expiry, dte, delta_estimate,
  iv_rank, indicator_readings, option_mid, order_id, order_status

New schema fields:
  strategy_name, signal_type, legs[], indicators{}, payload{},
  order_ids[], order_statuses[]

Usage:
    python scripts/migrate_signals_jsonl.py
    python scripts/migrate_signals_jsonl.py --input signals.jsonl --output signals.jsonl.new
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def _migrate_record(old: dict) -> dict:
    """Convert one old-schema signal dict to the new schema."""
    phase = old.get("phase", "NO_SIGNAL")
    signal_type = phase  # "SELL_PUT" | "SELL_CALL" | "NO_SIGNAL" stays the same

    strike = old.get("recommended_strike")
    expiry = old.get("recommended_expiry")
    delta = old.get("delta_estimate")
    option_mid = old.get("option_mid")
    dte = old.get("dte")
    iv_rank = old.get("iv_rank")
    old_order_id = old.get("order_id")
    old_order_status = old.get("order_status")

    # Build legs
    legs = []
    if signal_type in ("SELL_PUT", "SELL_CALL") and (strike is not None or expiry is not None):
        opt_type = "put" if signal_type == "SELL_PUT" else "call"
        leg: dict = {
            "asset_class": "option",
            "symbol": old.get("ticker", ""),
            "side": "sell",
            "qty": 1,
            "order_type": "limit",
            "limit_price": option_mid,
            "strike": strike,
            "expiry": expiry,
            "option_type": opt_type,
            "delta_estimate": delta,
        }
        legs.append(leg)

    # Build indicators from indicator_readings if present
    indicators: dict = {}
    ir = old.get("indicator_readings")
    if isinstance(ir, dict):
        for key in ("ema50", "ema50_slope", "rsi14", "bb_upper", "bb_lower",
                    "bb_percent_b", "volume_node_nearest", "volume_node_type"):
            if key in ir:
                indicators[key] = ir[key]

    # Build payload
    payload: dict = {}
    if dte is not None:
        payload["dte"] = dte
    if iv_rank is not None:
        payload["iv_rank"] = iv_rank

    new = {
        "strategy_name": "wheel",
        "ticker": old.get("ticker", ""),
        "signal_type": signal_type,
        "signal_timestamp": old.get("signal_timestamp", ""),
        "underlying_price": old.get("underlying_price", 0.0),
        "legs": legs,
        "indicators": indicators,
        "payload": payload,
        "thesis_text": old.get("thesis_text", ""),
        "confidence_tier": old.get("confidence_tier"),
        "confidence_rationale": old.get("confidence_rationale", ""),
        "risk_flags": old.get("risk_flags", []),
        "no_signal_reason": old.get("no_signal_reason"),
        "order_ids": [old_order_id] if old_order_id else [],
        "order_statuses": [old_order_status] if old_order_status else [],
    }
    return new


def migrate(input_path: Path, output_path: Path, backup_path: Path) -> int:
    lines = input_path.read_text(encoding="utf-8").splitlines()
    migrated = []
    skipped = 0

    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            old = json.loads(line)
            # Detect already-migrated records (have signal_type, not phase)
            if "signal_type" in old and "phase" not in old:
                migrated.append(json.dumps(old))
                continue
            new = _migrate_record(old)
            migrated.append(json.dumps(new))
        except Exception as exc:
            print(f"  Line {i}: skipped — {exc}")
            skipped += 1

    # Write new file
    output_path.write_text("\n".join(migrated) + ("\n" if migrated else ""), encoding="utf-8")

    # Backup original
    shutil.copy2(input_path, backup_path)
    print(f"Migrated {len(migrated)} records, skipped {skipped}.")
    print(f"Original backed up to: {backup_path}")
    print(f"New file written to:    {output_path}")
    return len(migrated)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate signals.jsonl to new schema.")
    parser.add_argument("--input",  default="signals.jsonl", help="Path to input JSONL")
    parser.add_argument("--output", default="", help="Path to output JSONL (default: overwrites input)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return

    output_path = Path(args.output) if args.output else input_path
    backup_path = input_path.with_suffix(".jsonl.legacy")

    migrate(input_path, output_path, backup_path)


if __name__ == "__main__":
    main()
