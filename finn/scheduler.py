"""In-app scheduler for a locally hosted FINN: runs finn.ingest at the same IST slots as GitHub Actions
(08:00, 16:30, 19:00, 22:00) and once at startup if a slot was missed while the app was off.

The run itself is idempotent and holds a Postgres advisory lock, so this, GitHub Actions and POST /tasks/ingest
never overlap. Off on Render (RENDER is set; GitHub Actions / cron-job.org trigger there) and when FINN_SCHEDULER=0.
Status at GET /api/v2/scheduler.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta

from .fiscal import IST

log = logging.getLogger("finn.scheduler")
state = {"enabled": False, "slots_ist": ["08:00", "16:30", "19:00", "22:00"], "last_run": None, "last_result": None,
         "last_error": None, "running": False, "next_run": None}


def enabled() -> bool:
    flag = os.getenv("FINN_SCHEDULER")
    if flag is not None:
        return flag == "1"
    return not os.getenv("RENDER")


def last_slot(now_ist: datetime) -> datetime:
    """The most recent slot at or before now (IST)."""
    from .ingest import SLOTS_IST
    for days_back in range(0, 3):
        d = (now_ist - timedelta(days=days_back)).date()
        for h, m in sorted(SLOTS_IST, reverse=True):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
            if t <= now_ist:
                return t
    raise RuntimeError("unreachable")


def next_slot(now_ist: datetime) -> datetime:
    from .ingest import SLOTS_IST
    for days_ahead in range(0, 2):
        d = (now_ist + timedelta(days=days_ahead)).date()
        for h, m in sorted(SLOTS_IST):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
            if t > now_ist:
                return t
    raise RuntimeError("unreachable")


def due(now_ist: datetime, last_run_ist: datetime | None) -> bool:
    return last_run_ist is None or last_run_ist < last_slot(now_ist)


def run_once(trigger: str = "scheduler") -> dict:
    from . import ingest
    state["running"] = True
    try:
        out = ingest.run(trigger=trigger)
        state.update(last_run=datetime.now(IST).isoformat(timespec="seconds"), last_result=_summary(out), last_error=None)
        return out
    except Exception as e:  # noqa: BLE001 - a failed run never takes the app down; the last good data keeps serving
        log.exception("ingest failed")
        state.update(last_error=f"{type(e).__name__}: {str(e)[:200]}", last_run=datetime.now(IST).isoformat(timespec="seconds"))
        return {"error": state["last_error"]}
    finally:
        state["running"] = False


def _summary(out: dict) -> dict:
    def added(section):
        sec = out.get(section) or {}
        return sum(v.get("added", 0) for v in sec.values() if isinstance(v, dict))
    return {"skipped": out.get("skipped"), "new_filings": added("announcements"), "price_rows": added("prices"),
            "events": added("events"), "alarm": (out.get("alarm") or {}).get("message"), "seconds": out.get("seconds")}


def _loop():
    last = None
    try:      # catch up only if a slot was missed since the last successful run (any process), not on every restart
        from .ingest import announcements_age
        age = announcements_age()
        last = datetime.now(IST) - age if age is not None else None
    except Exception:  # noqa: BLE001
        pass
    while True:
        now = datetime.now(IST)
        if due(now, last):
            run_once()
            last = datetime.now(IST)
        state["next_run"] = next_slot(datetime.now(IST)).isoformat(timespec="minutes")
        time.sleep(60)


def start():
    if state["enabled"] or not enabled():
        return
    state["enabled"] = True
    threading.Thread(target=_loop, daemon=True, name="finn-scheduler").start()
