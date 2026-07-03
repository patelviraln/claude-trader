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
from src.signal_engine import run_signals_batch
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
    """Execute one full signal scan — called by the scheduler or --run-now.

    Two config styles are supported:
      - strategies.toml (has a [router] section): tickers are grouped by their
        assigned strategy and each group is dispatched with that strategy.
      - legacy wheel.toml ([scheduler] tickers list): everything runs as Wheel.
    """
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    if "router" in raw:
        _run_job_routed(raw, config_path)
    else:
        _run_job_legacy_wheel(config_path)


def _run_exits(router_cfg: dict, adapter_name: str) -> None:
    """Phase 9: evaluate exit rules on open positions before scanning for entries."""
    if adapter_name != "alpaca":
        return  # fixture runs have no live account to manage
    rules = {**{"enabled": True}, **router_cfg.get("exit_rules", {})}
    if not rules.get("enabled", True):
        return
    try:
        from src.exit_engine import DEFAULT_RULES, run_exits
        from src.order_executor import AlpacaOrderExecutor
        executor = AlpacaOrderExecutor()
        decisions = run_exits(executor, rules={**DEFAULT_RULES, **rules}, router_cfg=router_cfg)
        log.info("scheduler.exits_done", closed=[f"{d.symbol}:{d.reason}" for d in decisions])
    except Exception as exc:
        log.error("scheduler.exits_error", error=str(exc))


def _run_job_routed(router_cfg: dict, config_path: Path) -> None:
    from src.router import grouped_assignments

    sched_cfg = router_cfg.get("scheduler", {})
    adapter_name: str = sched_cfg.get("adapter", "alpaca")
    groups = grouped_assignments(router_cfg)
    groups = {name: tickers for name, tickers in groups.items() if tickers}

    if not groups:
        log.warning("scheduler.no_tickers", msg="No ticker assignments in [router.assignments]")
        return

    log.info("scheduler.run_start", groups=groups, adapter=adapter_name)

    _run_exits(router_cfg, adapter_name)

    try:
        adapter = build_adapter(adapter_name)
    except Exception as exc:
        log.error("scheduler.adapter_error", error=str(exc))
        return

    results = {"ok": [], "error": []}
    for strategy_name, tickers in groups.items():
        for ticker in tickers:
            try:
                cards = run_signals_batch(
                    tickers=[ticker],
                    adapter=adapter,
                    print_output=True,
                    strategy_name=strategy_name,
                    router_config_path=config_path,
                )
                results["ok"].append(ticker)
                log.info("scheduler.ticker_done", ticker=ticker, strategy=strategy_name,
                         signal_type=cards[0].signal_type if cards else "?")
            except Exception as exc:
                results["error"].append(ticker)
                log.error("scheduler.ticker_error", ticker=ticker, strategy=strategy_name, error=str(exc))

    log.info("scheduler.run_complete", ok=results["ok"], errors=results["error"])


def _run_job_legacy_wheel(config_path: Path) -> None:
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
            log.info("scheduler.ticker_done", ticker=ticker, signal_type=cards[0].signal_type if cards else "?")
        except Exception as exc:
            results["error"].append(ticker)
            log.error("scheduler.ticker_error", ticker=ticker, error=str(exc))

    log.info(
        "scheduler.run_complete",
        ok=results["ok"],
        errors=results["error"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Signal Scheduler (multi-strategy)")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config TOML. Defaults to config/strategies.toml when present "
             "(router mode), otherwise config/wheel.toml (legacy Wheel-only mode).",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Fire the signal job once immediately and exit (useful for testing)",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)
    if args.config is not None:
        config_path = Path(args.config)
    elif Path("config/strategies.toml").exists():
        config_path = Path("config/strategies.toml")
    else:
        config_path = Path("config/wheel.toml")

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
