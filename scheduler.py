#!/usr/bin/env python3
"""
Daily signal scheduler — runs the Wheel strategy signal pipeline on a cron schedule.

Usage:
    python scheduler.py                        # uses config/wheel.toml [scheduler] section
    python scheduler.py --config path/to.toml  # custom config path
    python scheduler.py --run-now              # fire once immediately then exit

The scheduler runs every trading day at the configured time (default 9:35 AM ET).
All scheduler settings live in the [scheduler] section of wheel.toml.
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.logger import setup_logging
import structlog

log = structlog.get_logger()


def load_scheduler_config(config_path: str | Path = "config/wheel.toml") -> dict:
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    return raw.get("scheduler", {})


def build_adapter(adapter_name: str):
    if adapter_name == "fixture":
        from src.adapters.fixture_adapter import FixtureAdapter
        return FixtureAdapter()
    elif adapter_name == "alpaca":
        from src.adapters.alpaca_adapter import AlpacaPaperAdapter
        return AlpacaPaperAdapter()
    else:
        raise ValueError(f"Unknown adapter: {adapter_name}")


def run_job(config_path: Path) -> None:
    """Execute one full signal scan — called by the scheduler or --run-now."""
    sched_cfg = load_scheduler_config(config_path)
    tickers: list[str] = sched_cfg.get("tickers", [])
    adapter_name: str = sched_cfg.get("adapter", "alpaca")

    if not tickers:
        log.warning("scheduler.no_tickers", msg="No tickers configured in [scheduler] section")
        return

    log.info("scheduler.run_start", tickers=tickers, adapter=adapter_name)

    try:
        adapter = build_adapter(adapter_name)
    except Exception as exc:
        log.error("scheduler.adapter_error", error=str(exc))
        return

    from src.signal_engine import run_signals_batch

    results = {"ok": [], "error": []}
    for ticker in tickers:
        try:
            cards = run_signals_batch(
                tickers=[ticker],
                adapter=adapter,
                config_path=config_path,
                print_output=True,
            )
            results["ok"].append(ticker)
            log.info("scheduler.ticker_done", ticker=ticker, phase=cards[0].phase if cards else "?")
        except Exception as exc:
            results["error"].append(ticker)
            log.error("scheduler.ticker_error", ticker=ticker, error=str(exc))

    log.info(
        "scheduler.run_complete",
        ok=results["ok"],
        errors=results["error"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Wheel Strategy Daily Scheduler")
    parser.add_argument("--config", default="config/wheel.toml", help="Path to wheel config TOML")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Fire the signal job once immediately and exit (useful for testing)",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)
    config_path = Path(args.config)

    if args.run_now:
        log.info("scheduler.manual_trigger")
        run_job(config_path)
        return

    # --- APScheduler cron setup ---
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print("APScheduler not installed. Run: pip install apscheduler", file=sys.stderr)
        sys.exit(1)

    sched_cfg = load_scheduler_config(config_path)
    run_hour: int = sched_cfg.get("run_hour", 9)
    run_minute: int = sched_cfg.get("run_minute", 35)
    tz: str = sched_cfg.get("timezone", "America/New_York")

    scheduler = BlockingScheduler(timezone=tz)
    trigger = CronTrigger(
        day_of_week="mon-fri",
        hour=run_hour,
        minute=run_minute,
        timezone=tz,
    )
    scheduler.add_job(run_job, trigger, args=[config_path], id="daily_signals")

    log.info(
        "scheduler.started",
        run_time=f"{run_hour:02d}:{run_minute:02d} {tz}",
        days="Mon-Fri",
    )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("scheduler.stopped")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
