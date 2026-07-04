"""
Run watchdog — alerts when the daily pipeline did NOT run.

The daily summary only fires when a run completes, so a sleeping laptop,
broken environment, or unregistered task fails silently. This checks the
heartbeat the scheduler writes (logs/last_run.json) and pushes a warning
when today's run is missing or ended in an error.

Scheduled Mon-Fri shortly after the run window by register_daily_task.ps1:
    python -m src.watchdog
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import structlog

import src.logger as _log_setup  # noqa: F401

logger = structlog.get_logger(__name__)

HEARTBEAT_PATH = Path("logs/last_run.json")


def load_heartbeat(path: str | Path = HEARTBEAT_PATH) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def check_run(
    heartbeat: dict | None,
    today: date | None = None,
) -> str | None:
    """Return an alert message when today's run is missing/failed, else None.

    Weekends never alert (the task only fires Mon-Fri).
    """
    today = today or date.today()
    if today.weekday() >= 5:  # Sat/Sun
        return None

    if heartbeat is None:
        return ("No daily run has EVER completed (no heartbeat file). "
                "Check the ClaudeTraderDaily scheduled task and logs/scheduler_runs.log.")

    if heartbeat.get("date") != today.isoformat():
        return (f"No daily run today ({today.isoformat()}). Last successful heartbeat: "
                f"{heartbeat.get('date', 'unknown')}. Machine asleep at run time? "
                "Check the ClaudeTraderDaily task and logs/scheduler_runs.log.")

    if not heartbeat.get("ok", True):
        return (f"Today's run CRASHED: {heartbeat.get('detail', 'unknown error')}. "
                "See logs/scheduler_runs.log.")

    return None


def main() -> int:
    from src.notifier import send_summary

    alert = check_run(load_heartbeat())
    if alert is None:
        logger.info("watchdog.ok")
        return 0

    logger.warning("watchdog.alert", message=alert)
    send_summary("Claude Trader — WATCHDOG", f"⚠️ {alert}")
    return 1


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    raise SystemExit(main())
