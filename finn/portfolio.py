"""Add or remove a holding from Settings.

add(symbol):    validate the ticker (NSE's official equity list -> name and ISIN; Yahoo Finance -> price history;
                BSE's securities list by ISIN -> BSE code, optional), save it as a holding, then onboard it in the
                background with visible stages: filings and prices, reading the new filings, a thesis draft from
                management's own claims, and the research report. Progress at GET /api/v2/portfolio/jobs/{symbol}.
remove(symbol): hides the company from Today, Holdings and alerts (status 'removed', not a holding); every filing,
                card, report and alert is kept, and adding it again restores it.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .database import session
from .models import Company

log = logging.getLogger("finn.portfolio")
NSE_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
STAGES = ["Checking the ticker", "Fetching filings and prices", "Reading the new filings", "Building the research report",
          "Drafting a thesis"]
RESEARCH_WAIT_S = 300
JOBS: dict[str, dict] = {}
_nse_cache: dict = {}


class InvalidTicker(ValueError):
    pass


def nse_listing(symbol: str) -> dict | None:
    """{symbol, name, isin} from NSE's official equity list (cached for a day)."""
    from .sources.chain import http
    if not _nse_cache or time.time() - _nse_cache.get("_at", 0) > 86400:
        r = http().get(NSE_LIST, referer="https://www.nseindia.com/", timeout=30, attempts=2)
        if r.status_code != 200:
            raise RuntimeError(f"NSE equity list unavailable (HTTP {r.status_code})")
        rows = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            row = {k.strip(): (v or "").strip() for k, v in row.items()}
            if row.get("SERIES") in ("EQ", "BE"):
                rows[row["SYMBOL"].upper()] = {"symbol": row["SYMBOL"].upper(), "name": row["NAME OF COMPANY"],
                                               "isin": row["ISIN NUMBER"]}
        _nse_cache.clear()
        _nse_cache.update(rows=rows, _at=time.time())
    return _nse_cache["rows"].get(symbol.upper())


def bse_code_for(isin: str) -> str | None:
    import tempfile
    from bse import BSE
    try:
        with BSE(download_folder=tempfile.gettempdir()) as b:
            for group in ("A", "B"):
                for r in b.listSecurities(group=group):
                    if r.get("ISIN_NUMBER") == isin:
                        return str(r["SCRIP_CD"])
                time.sleep(1.0)
    except Exception as e:  # noqa: BLE001 - a BSE code is optional; NSE sources cover filings without one
        log.warning("BSE code lookup failed for %s: %s", isin, e)
    return None


def validate(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9&-]{1,20}", sym):
        raise InvalidTicker("Enter an NSE symbol, e.g. HAVELLS")
    listing = nse_listing(sym)
    if not listing:
        raise InvalidTicker(f"{sym} is not on NSE's list of listed equities")
    from .sources import prices
    bars = prices.frame_to_rows(prices.fetch(f"{sym}.NS", 10), f"{sym}.NS", None)
    if not bars:
        raise InvalidTicker(f"No recent NSE price history for {sym} on Yahoo Finance")
    return {**listing, "yahoo_symbol": f"{sym}.NS", "bse_code": bse_code_for(listing["isin"]), "last_close": bars[-1]["close"]}


def add(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    job = JOBS.get(sym)
    if job and not job.get("done"):
        return job
    JOBS[sym] = job = {"symbol": sym, "stages": STAGES, "stage": STAGES[0], "done": False, "error": None,
                       "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "log": []}
    try:
        info = validate(sym)
    except InvalidTicker as e:
        job.update(done=True, error=str(e))
        return job
    except Exception as e:  # noqa: BLE001
        job.update(done=True, error=f"Couldn't check {sym} right now: {str(e)[:120]}")
        return job
    with session() as s:
        c = s.scalar(select(Company).where(Company.nse_symbol == sym))
        if c is None:
            c = Company(nse_symbol=sym, name=info["name"], isin=info.get("isin"), bse_code=info.get("bse_code"),
                        yahoo_symbol=info["yahoo_symbol"], is_portfolio=True, status="ok")
            s.add(c)
            job["log"].append(f"Added {info['name']}" + (f" (BSE {info['bse_code']})" if info.get("bse_code") else
                                                       " (no BSE listing found: filings come from NSE)"))
        else:
            c.is_portfolio, c.status = True, "ok"
            job["log"].append(f"Restored {c.name}: its history was kept")
    threading.Thread(target=_onboard, args=(sym,), daemon=True, name=f"onboard-{sym}").start()
    return job


def _onboard(sym: str) -> None:
    job = JOBS[sym]

    def stage(i, note=None):
        job["stage"] = STAGES[i]
        if note:
            job["log"].append(note)
    try:
        with session() as s:
            c = s.scalar(select(Company).where(Company.nse_symbol == sym))
            s.expunge(c)
        from .sources import chain
        stage(1)
        a = chain.announcements([c], 90)
        p = chain.price_chain([c], 370)
        chain.snapshot_step([c])
        stage(2, f"{sum(v.get('added', 0) for v in a.values() if isinstance(v, dict))} filings and "
                 f"{sum(v.get('added', 0) for v in p.values() if isinstance(v, dict))} price rows fetched")
        from . import pipeline
        pipeline.run(rescore_all=False)
        stage(3)                                     # research first: the thesis draft reads its verified statements
        from .research import engine
        r = engine.start(sym)
        job["research_job"] = r.get("job_id")
        if not r.get("cached") and r.get("job_id"):
            from .models import ResearchJob
            t0 = time.time()
            while time.time() - t0 < RESEARCH_WAIT_S:
                with session() as s:
                    rj = s.get(ResearchJob, r["job_id"])
                    st, err = (rj.status, rj.error) if rj else ("missing", None)
                if st in ("done", "failed", "missing"):
                    break
                time.sleep(3)
            job["log"].append("Research report ready" if st == "done" else
                              f"Research report not finished ({err or st}); it can be run from the Research tab")
        else:
            job["log"].append("Research report ready")
        stage(4)
        from .thesis import guardian
        with session() as s:
            c = s.scalar(select(Company).where(Company.nse_symbol == sym))
            s.expunge(c)
        d = guardian.draft(c)
        job["log"].append(f"No thesis draft: {d['skipped']}" if d.get("skipped") else
                          f"Thesis drafted from management's own claims ({d.get('kpis', 0)} numbers to watch)")
        job.update(done=True, finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as e:  # noqa: BLE001 - the company stays added; the next scheduled run fills what failed
        log.exception("onboarding %s failed", sym)
        job.update(done=True, error=f"{job['stage']} failed: {str(e)[:160]}. The company is added; the next scheduled "
                                    "refresh fills in what is missing.")


def remove(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    with session() as s:
        c = s.scalar(select(Company).where(Company.nse_symbol == sym))
        if not c:
            raise InvalidTicker(f"{sym} is not a company FINN tracks")
        c.is_portfolio, c.status = False, "removed"
    from .feed import digest
    digest.invalidate()
    return {"symbol": sym, "removed": True, "kept": "filings, cards, reports and alerts are kept; add it again to restore"}
