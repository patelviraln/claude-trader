"""
Phase-14-lite daily summary notifications.

Transport is chosen by environment:
  NTFY_TOPIC set  → push via https://ntfy.sh/{topic} (subscribe in the ntfy
                    mobile/web app — zero-signup pub/sub)
  otherwise       → appended to logs/daily_summary.md so unattended runs
                    always leave a readable trail.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

import src.logger as _log_setup  # noqa: F401

logger = structlog.get_logger(__name__)

SUMMARY_LOG_PATH = Path("logs/daily_summary.md")


def send_summary(title: str, body: str) -> str:
    """Send a summary via the configured transport; returns which one was used."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        try:
            import requests
            resp = requests.post(
                f"https://ntfy.sh/{topic}",
                data=body.encode("utf-8"),
                headers={"Title": title},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("notify.sent", transport="ntfy", topic=topic)
            return "ntfy"
        except Exception as exc:
            logger.error("notify.ntfy_failed", error=str(exc))
            # fall through to file log so the summary is never lost

    p = SUMMARY_LOG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"\n## {title} — {stamp}\n\n{body}\n")
    logger.info("notify.sent", transport="file", path=str(p))
    return "file"


def build_run_summary(
    exit_decisions: list,
    cards_by_ticker: dict[str, str],
    reconcile_changes: list[dict],
    realized: dict | None = None,
    errors: list[str] | None = None,
) -> str:
    """Assemble the end-of-run summary body from scheduler results."""
    lines: list[str] = []

    actionable = {t: s for t, s in cards_by_ticker.items() if s not in ("NO_SIGNAL", "?")}
    lines.append(f"Signals: {len(cards_by_ticker)} scanned, {len(actionable)} actionable")
    for t, s in sorted(actionable.items()):
        lines.append(f"  • {t}: {s}")

    if exit_decisions:
        lines.append(f"Exits: {len(exit_decisions)} closed")
        for d in exit_decisions:
            lines.append(f"  • {d.symbol}: {d.reason}")
    else:
        lines.append("Exits: none fired")

    if reconcile_changes:
        lines.append(f"State synced: {len(reconcile_changes)} ticker(s) reconciled")
        for c in reconcile_changes:
            lines.append(f"  • {c['ticker']} ({c['strategy']})")

    if realized is not None:
        lines.append(f"Realized P&L (all time): ${realized.get('total', 0.0):,.2f}")
        for strat, pnl in sorted((realized.get("by_strategy") or {}).items()):
            lines.append(f"  • {strat}: ${pnl:,.2f}")

    if errors:
        lines.append(f"Errors: {len(errors)}")
        for e in errors[:5]:
            lines.append(f"  • {e}")

    return "\n".join(lines)
