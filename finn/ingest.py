"""One scheduled run: ingest (announcements, prices, events) -> pipeline (classify, score, enrich, thesis) -> alerts
-> push. Idempotent and never overlapping: a Postgres advisory lock is held for the whole run, so GitHub Actions,
cron-job.org (POST /tasks/ingest) and a locally hosted app can all trigger it safely.

Schedule (IST): 08:00, 16:30, 19:00, 22:00 — morning catch-up, then after the market, after the evening filings,
and late night. Each run looks back LOOKBACK_DAYS, so a missed run is caught by the next one.

    python -m finn.ingest                 # one scheduled run
    python -m finn.ingest --backfill 90   # one-off backfill (announcements, prices, events)
    python -m finn.ingest --probe         # one small request per source, prints a table (GitHub probe job)

Exit code 1 when every announcement source has been failing for 24 h (the GitHub job then fails visibly, and a
push is sent to the owner).
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

log = logging.getLogger("finn.ingest")
LOCK_ROW = "__run_lock__"
LOCK_LEASE = timedelta(minutes=90)        # longer than any run (GitHub job timeout is 45 min)
LOOKBACK_DAYS = 3
STALE_ALARM = timedelta(hours=24)
SLOTS_IST = [(8, 0), (16, 30), (19, 0), (22, 0)]
state = {"running": False, "last": None}


@contextmanager
def run_lock(lease: timedelta = LOCK_LEASE):
    """Yields True if this process holds the run lock. A lease row in source_health ('__run_lock__'), taken with one
    atomic UPDATE ... WHERE the lease has expired. Works through Neon's pooled endpoint (session advisory locks do
    not survive PgBouncer, and an idle locking connection is killed by idle_in_transaction_session_timeout), and on
    SQLite. A crashed run frees the lock when its lease expires."""
    from sqlalchemy import or_, update
    from .database import session
    from .models import SourceHealth
    from .sources.common import insert_ignore
    holder = f"{uuid.uuid4()}"
    insert_ignore(SourceHealth, [{"source": LOCK_ROW, "kind": "lock", "consecutive_failures": 0, "last_added": 0,
                                  "last_seen": 0, "total_added": 0}], ["source"])
    now = datetime.now(timezone.utc)
    with session() as s:
        got = s.execute(update(SourceHealth).where(SourceHealth.source == LOCK_ROW,
                                                   or_(SourceHealth.open_until.is_(None), SourceHealth.open_until < now))
                        .values(open_until=now + lease, last_error=holder)).rowcount == 1
    try:
        yield got
    finally:
        if got:
            with session() as s:
                s.execute(update(SourceHealth).where(SourceHealth.source == LOCK_ROW, SourceHealth.last_error == holder)
                          .values(open_until=None, last_success_at=datetime.now(timezone.utc)))


def companies(include_hidden: bool = False):
    from .database import session
    from .models import Company
    with session() as s:
        cos = list(s.scalars(select(Company).order_by(Company.is_portfolio.desc(), Company.nse_symbol)))
        for c in cos:
            s.expunge(c)
    return [c for c in cos if include_hidden or c.status != "removed"]


def announcements_age() -> timedelta | None:
    """Time since any announcement source last succeeded (None = never)."""
    from .database import session
    from .models import SourceHealth
    with session() as s:
        last = s.scalar(select(SourceHealth.last_success_at).where(SourceHealth.kind == "announcements")
                        .order_by(SourceHealth.last_success_at.desc().nullslast()).limit(1))
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last if last else None


_fresh_cache: dict = {}
FRESH_TTL_S = 60


def freshness() -> dict:
    """Cached for a minute (it only changes when a run finishes, which clears it)."""
    hit = _fresh_cache.get("v")
    if hit and time.time() - hit[0] < FRESH_TTL_S:
        return hit[1]
    out = _freshness()
    _fresh_cache["v"] = (time.time(), out)
    return out


def _freshness() -> dict:
    """'Data updated Xh ago': the last time any announcement source succeeded. Amber (stale) only when that is more
    than 26 h ago on a trading day; quiet weekends and holidays are not stale."""
    from .fiscal import IST, is_trading_day
    age = announcements_age()
    hours = round(age.total_seconds() / 3600, 1) if age is not None else None
    trading = is_trading_day(datetime.now(IST).date())
    return {"data_updated_at": (datetime.now(timezone.utc) - age).isoformat(timespec="seconds") if age is not None else None,
            "hours_ago": hours, "trading_day": trading, "stale": hours is None or (hours > 26 and trading)}


def alarm_if_stale() -> dict | None:
    """All announcement sources failing for 24 h: push the owner once per run that sees it, and report it."""
    age = announcements_age()
    if age is not None and age < STALE_ALARM:
        return None
    msg = ("FINN has had no announcements from BSE or NSE for "
           + (f"{age.total_seconds() / 3600:.0f} hours" if age else "ever") + ". Check /health/data.")
    try:
        from .alerting import notify
        sent = notify.send_to_all("FINN: data sources down", msg, "/#settings") if notify.configured() else {"skipped": "no VAPID"}
    except Exception as e:  # noqa: BLE001
        sent = {"error": str(e)[:200]}
    log.error(msg)
    return {"message": msg, "push": sent}


def run(days: int = LOOKBACK_DAYS, trigger: str = "manual", pipeline: bool = True) -> dict:
    with run_lock() as got:
        if not got:
            return {"skipped": "another run holds the lock", "trigger": trigger}
        state["running"] = True
        t0, out = time.time(), {"trigger": trigger, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            from .sources import chain
            cos = companies()
            for name, fn in [("announcements", lambda: chain.announcements(cos, days)),
                             ("prices", lambda: chain.price_chain(cos, max(days, 10))),
                             ("events", lambda: chain.event_chain(cos)),
                             ("snapshots", lambda: chain.snapshot_step(cos))]:
                try:
                    out[name] = fn()
                except Exception as e:  # noqa: BLE001 - one stage failing never stops the others
                    log.exception("%s failed", name)
                    out[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            if pipeline:
                from . import pipeline as pl
                out["pipeline"] = pl.run(rescore_all=False)
                out["pdf_duplicates_linked"] = chain.link_pdf_duplicates()
            out["alarm"] = alarm_if_stale()
        finally:
            out["seconds"] = round(time.time() - t0, 1)
            state.update(running=False, last=out)
            _fresh_cache.clear()
            from .sources import chain as _chain
            _chain.health_cache.clear()
        return out


def backfill_results(days: int = 365, pipeline: bool = True) -> dict:
    """One-off: a year of results filings (BSE category 'Result') for every company. Four quarterly filings carry
    eight distinct quarters (each prints the quarter, the one before and the year-ago quarter), which the 8-quarter
    results chart needs. Polite: one BSE call per company. Then the pipeline extracts them like any filing."""
    from .sources import bse_filings, chain
    with run_lock() as got:
        if not got:
            return {"skipped": "another run holds the lock"}
        cos = [c for c in companies() if c.bse_code]
        rows, failed = bse_filings.fetch(cos, days, category="Result")
        at = datetime.now(timezone.utc)
        good = [r for r in rows if chain.valid_filing(r, {c.id for c in cos}, at)]
        out = {"companies": len(cos), "rows": len(good), "added": chain.write_filings(good) if good else 0,
               "failed": [c.nse_symbol for c in failed]}
    if pipeline:
        from . import pipeline as pl
        out["pipeline"] = pl.run(rescore_all=False)
    return out


def probe() -> list[dict]:
    """One small request per source; never writes. Used by the GitHub probe job to test cloud IPs."""
    import tempfile
    from datetime import date
    from .sources import chain, nse
    from .sources.bse_filings import last_refusal
    h, rows = chain.http(), []

    def t(name, fn):
        t1 = time.time()
        try:
            detail = fn()
            rows.append({"source": name, "ok": True, "detail": detail, "ms": round(1000 * (time.time() - t1))})
        except Exception as e:  # noqa: BLE001
            rows.append({"source": name, "ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}",
                         "refusal": dict(last_refusal) or None, "ms": round(1000 * (time.time() - t1))})

    def bse():
        from bse import BSE
        from .sources.bse_filings import _record_refusal
        with BSE(download_folder=tempfile.gettempdir()) as b:
            b.session.hooks["response"].append(_record_refusal)
            res = b.announcements(scripcode="532540", from_date=datetime.now() - timedelta(days=3), to_date=datetime.now())
        return f"{len(res.get('Table') or [])} rows"

    def get(url):
        r = h.get(url, referer=nse.REFERER, timeout=30, attempts=1)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]!r}")
        try:
            body = r.json()
            return f"HTTP 200, {len(body)} rows" if isinstance(body, list) else f"HTTP 200, {len(r.content)} bytes"
        except ValueError:
            return f"HTTP 200, {len(r.content)} bytes"

    def yf():
        from .sources import prices
        n = len(prices.fetch("TCS.NS", 5))
        if not n:
            raise RuntimeError("no bars")
        return f"{n} bars"

    def bhav():
        for back in range(7):
            d = date.today() - timedelta(days=back)
            if d.weekday() < 5:
                r = h.get(nse.BHAV.format(d=d), referer=nse.REFERER, timeout=30, attempts=1)
                if r.status_code == 200:
                    return f"{d}: {len(r.content)} bytes"
        raise RuntimeError("no file in the last 7 days")

    t("bse_announcements", bse)
    t("nse_api_announcements", lambda: get(f"{nse.API}?index=equities&symbol=TCS"
                                           f"&from_date={date.today() - timedelta(days=3):%d-%m-%Y}&to_date={date.today():%d-%m-%Y}"))
    t("nse_rss_announcements", lambda: get(nse.RSS))
    t("nse_events", lambda: get(nse.EVENTS))
    t("yfinance_prices", yf)
    t("nse_bhavcopy_prices", bhav)
    return rows


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    if "--probe" in argv:
        res = probe()
        for r in res:
            print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['source']:<24} {r['ms']:>6} ms  {r['detail']}")
            if r.get("refusal"):
                print("      refusal:", json.dumps(r["refusal"]))
        return 0
    if "--backfill-results" in argv:
        print(json.dumps(backfill_results(), indent=1, default=str))
        return 0
    if "--backfill" in argv:
        days = int(argv[argv.index("--backfill") + 1])
        res = run(days=days, trigger=f"backfill {days}d", pipeline="--no-pipeline" not in argv)
    else:
        res = run(trigger="cli", pipeline="--no-pipeline" not in argv)
    print(json.dumps(res, indent=1, default=str))
    return 1 if res.get("alarm") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
