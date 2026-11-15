"""Precompute research reports (and the deep dives for the showcase) so a demo never waits on a cold report.

Resumable and quota-aware: works through companies in priority order, skips any with a report younger than
MAX_AGE, and stops cleanly when every model is out of today's free-tier quota; the next run carries on. Runs after
the 22:00 IST ingest (GitHub Actions), or by hand:

    python -m finn.precompute                # holdings first, then the large caps below
    python -m finn.precompute --limit 3      # at most 3 reports this run
    python -m finn.precompute --deep POLYCAB # all deep dives for one company
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

log = logging.getLogger("finn.precompute")
MAX_AGE = timedelta(days=7)
LARGE_CAPS = ["RELIANCE", "INFY", "ICICIBANK", "HINDUNILVR", "BHARTIARTL", "ITC", "SBIN", "KOTAKBANK", "AXISBANK", "ULTRACEMCO"]


def _age(symbol: str) -> timedelta | None:
    from .database import session
    from .models import Company, Report
    with session() as s:
        at = s.scalar(select(Report.created_at).join(Company, Company.id == Report.company_id)
                      .where(Company.nse_symbol == symbol, Report.kind == "core").order_by(Report.created_at.desc()).limit(1))
    if at is None:
        return None
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at


def queue() -> list[str]:
    from .ingest import companies
    holdings = [c.nse_symbol for c in companies() if c.is_portfolio]
    return holdings + [s for s in LARGE_CAPS if s not in holdings]


def run(limit: int | None = None, symbols: list[str] | None = None) -> dict:
    from .llm import router
    from .research import engine
    done, skipped, failed, t0 = [], [], {}, time.time()
    for sym in symbols or queue():
        if limit is not None and len(done) >= limit:
            break
        age = _age(sym)
        if age is not None and age < MAX_AGE:
            skipped.append(sym)
            continue
        if not router.pool("cheap"):
            failed["_stopped"] = "every cheap-tier model is out of today's quota; the next run carries on"
            break
        t1 = time.time()
        try:
            job = engine.start(sym, force=age is not None)
            if job.get("cached"):
                done.append({"symbol": sym, "seconds": 0, "cached": True})
                continue
            out = _wait(job["job_id"])
            if out == "done":
                done.append({"symbol": sym, "seconds": round(time.time() - t1, 1)})
            else:
                failed[sym] = out
        except Exception as e:  # noqa: BLE001 - one company never stops the queue
            failed[sym] = f"{type(e).__name__}: {str(e)[:160]}"
            if "out of quota" in str(e) or "all cheap-tier models failed" in str(e):
                failed["_stopped"] = "models out of quota"
                break
    return {"built": done, "fresh_already": skipped, "failed": failed, "seconds": round(time.time() - t0, 1)}


def _wait(job_id: str, timeout_s: int = 600) -> str:
    from .database import session
    from .models import ResearchJob
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        with session() as s:
            j = s.get(ResearchJob, job_id)
            st, err = (j.status, j.error) if j else ("missing", None)
        if st == "done":
            return "done"
        if st in ("failed", "missing"):
            return f"failed: {err or st}"
        time.sleep(3)
    return "timed out"


def deep_all(symbol: str) -> dict:
    from .deep import engine as deep
    out = {}
    for r in deep.RECIPES:
        t1 = time.time()
        try:
            d = deep.run(symbol, r)
            out[r] = {"ok": True, "cached": d.get("cached"), "seconds": round(time.time() - t1, 1)}
        except Exception as e:  # noqa: BLE001
            out[r] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return out


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    if "--deep" in argv:
        print(json.dumps(deep_all(argv[argv.index("--deep") + 1].upper()), indent=1))
        return 0
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    syms = [a.upper() for a in argv if not a.startswith("--") and not a.isdigit()] or None
    print(json.dumps(run(limit, syms), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
