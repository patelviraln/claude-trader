"""Unit tests for src/watchdog.py — missing-run detection."""
from __future__ import annotations

import json
from datetime import date

from src.watchdog import check_run, load_heartbeat

MONDAY = date(2026, 7, 6)
SATURDAY = date(2026, 7, 4)


def _beat(d: date = MONDAY, ok: bool = True, detail: str = "") -> dict:
    return {"date": d.isoformat(), "ts": f"{d.isoformat()}T13:40:00+00:00",
            "ok": ok, "detail": detail}


class TestCheckRun:
    def test_ok_when_ran_today(self):
        assert check_run(_beat(MONDAY), today=MONDAY) is None

    def test_alerts_when_no_heartbeat_ever(self):
        msg = check_run(None, today=MONDAY)
        assert msg is not None and "EVER" in msg

    def test_alerts_when_stale(self):
        msg = check_run(_beat(date(2026, 7, 3)), today=MONDAY)
        assert msg is not None and "No daily run today" in msg
        assert "2026-07-03" in msg

    def test_alerts_when_run_crashed(self):
        msg = check_run(_beat(MONDAY, ok=False, detail="boom"), today=MONDAY)
        assert msg is not None and "CRASHED" in msg and "boom" in msg

    def test_weekends_never_alert(self):
        assert check_run(None, today=SATURDAY) is None
        assert check_run(_beat(date(2026, 7, 1)), today=SATURDAY) is None


class TestLoadHeartbeat:
    def test_missing_file(self, tmp_path):
        assert load_heartbeat(tmp_path / "nope.json") is None

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "last_run.json"
        p.write_text(json.dumps(_beat()), encoding="utf-8")
        hb = load_heartbeat(p)
        assert hb["date"] == MONDAY.isoformat()
        assert hb["ok"] is True

    def test_corrupt_file(self, tmp_path):
        p = tmp_path / "last_run.json"
        p.write_text("not-json", encoding="utf-8")
        assert load_heartbeat(p) is None
