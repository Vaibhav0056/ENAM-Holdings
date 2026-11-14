"""In-app refresh scheduler, so a locally hosted FINN keeps itself current.

- Startup: one catch-up run (filings, prices, calendar, pipeline) for anything missed while the app was off.
- Trading days, 08:00-21:00 IST, every FINN_REFRESH_MINUTES (default 30): new BSE filings + the pipeline
  (classify, score, enrich, thesis, alerts, push). Filings are content-hashed and model answers cached, so a quiet
  half-hour costs no model calls.
- Every evening after 18:30 IST, once a day: prices, BSE snapshot and the catalysts calendar.

Runs never overlap. Off on Render (the GitHub Actions workflow does the daily refresh there) and when
FINN_SCHEDULER=0. Status at GET /api/v2/scheduler.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from .fiscal import IST, is_trading_day

log = logging.getLogger("finn.scheduler")
_lock = threading.Lock()
state = {"enabled": False, "interval_min": None, "last_run": None, "last_result": None, "last_error": None,
         "running": False, "last_daily": None, "next_check": None, "bse_paused_until": None}
BSE_BACKOFF = timedelta(hours=3)


def enabled() -> bool:
    flag = os.getenv("FINN_SCHEDULER")
    if flag is not None:
        return flag == "1"
    return not os.getenv("RENDER")          # Render sets RENDER=true; GitHub Actions refreshes there


def interval_minutes() -> int:
    try:
        return max(5, int(os.getenv("FINN_REFRESH_MINUTES", "30")))
    except ValueError:
        return 30


def in_window(now_ist: datetime) -> bool:
    return is_trading_day(now_ist.date()) and 8 <= now_ist.hour < 21


def needs_daily(now_ist: datetime, last_daily: str | None) -> bool:
    return (now_ist.hour, now_ist.minute) >= (18, 30) and last_daily != now_ist.date().isoformat()


def run_once(daily: bool) -> dict:
    """One refresh. daily=True also refreshes prices, BSE snapshot and the calendar."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "a refresh is already running"}
    state["running"] = True
    t0 = time.time()
    try:
        from .database import session
        from .models import Company
        from sqlalchemy import select
        from .sources import bse_filings, bse_snapshot, calendar, prices
        with session() as s:
            cos = list(s.scalars(select(Company)))
            for c in cos:
                s.expunge(c)
        now = datetime.now(timezone.utc)
        bse_ok = not state["bse_paused_until"] or now >= datetime.fromisoformat(state["bse_paused_until"])
        if bse_ok:
            res = bse_filings.ingest(cos, 7)
            refused = sum(1 for v in res.values() if "403" in str(v.get("error", "")))
            out = {"new_filings": sum(v.get("added", 0) for v in res.values())}
            if refused >= max(1, len(res) // 2):
                # BSE is refusing FINN's requests. Back off instead of retrying every half hour, and never
                # disguise the client to get past it.
                state["bse_paused_until"] = (now + BSE_BACKOFF).isoformat(timespec="minutes")
                out["bse"] = f"BSE refused {refused} of {len(res)} requests (403); paused until {state['bse_paused_until']}"
            else:
                state["bse_paused_until"] = None
        else:
            out = {"new_filings": 0, "bse": f"paused after BSE refused requests; resumes {state['bse_paused_until']}"}
        if daily and bse_ok and not state["bse_paused_until"]:
            bse_snapshot.ingest(cos)
            out["calendar"] = calendar.ingest(cos).get("added")
        if daily:   # prices come from Yahoo Finance, not BSE
            p = prices.ingest(cos, 30)
            out["price_rows_added"] = sum(v.get("added", 0) for v in p.values())
        from . import pipeline
        res = pipeline.run(rescore_all=False)
        feed = res.get("feed") if isinstance(res.get("feed"), dict) else {}
        out.update(scored=feed.get("scored"), new_classified=feed.get("new"), alerts=res.get("alerts"),
                   pushed=(res.get("notify") or {}).get("sent") if isinstance(res.get("notify"), dict) else None)
        out["seconds"] = round(time.time() - t0, 1)
        state.update(last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"), last_result=out, last_error=None)
        log.warning("refresh done: %s", out)
        return out
    except Exception as e:  # noqa: BLE001 - a failed refresh never takes the app down
        log.exception("refresh failed")
        state.update(last_error=f"{type(e).__name__}: {str(e)[:200]}", last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return {"error": state["last_error"]}
    finally:
        state["running"] = False
        _lock.release()


def _loop():
    every = interval_minutes() * 60
    first = True
    while True:
        now = datetime.now(IST)
        daily = first or needs_daily(now, state["last_daily"])
        if first or daily or in_window(now):
            run_once(daily)
            if daily and not first or (first and (now.hour, now.minute) >= (18, 30)):
                state["last_daily"] = now.date().isoformat()
        first = False
        state["next_check"] = datetime.fromtimestamp(time.time() + every, IST).isoformat(timespec="minutes")
        time.sleep(every)


def start():
    if state["enabled"] or not enabled():
        return
    state.update(enabled=True, interval_min=interval_minutes())
    threading.Thread(target=_loop, daemon=True, name="finn-scheduler").start()
