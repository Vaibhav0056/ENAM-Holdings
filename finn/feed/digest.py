"""The Today feed: one precomputed query, zero LLM calls.

Windows: presets 24h / 3d / 7d / 30d, "since I last checked", or a custom IST date range of at most 90 days.
`as_of` replays the feed as it would have looked at a past moment (time-travel, Phase 10): only filings filed by then.
Holdings only; research peers surface through peer read-across alerts instead.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from ..database import read_session_run, session
from ..fiscal import IST, ist_day_bounds, to_ist
from ..models import Company, Enrichment, Filing, UserState

PRESETS = {"24h": timedelta(hours=24), "3d": timedelta(days=3), "7d": timedelta(days=7), "30d": timedelta(days=30)}
MAX_RANGE = timedelta(days=90)
CACHE_TTL = 60.0

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()
_version = 0


def invalidate():
    """Called after anything that changes the feed in this process (pipeline run, review, feedback)."""
    global _version
    with _lock:
        _version += 1
        _cache.clear()


class WindowError(ValueError):
    pass


def resolve_window(window: str | None = "24h", start: str | None = None, end: str | None = None,
                   now: datetime | None = None, last_checked: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if start or end:
        if not (start and end):
            raise WindowError("a custom range needs both from and to")
        try:
            d0, d1 = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        except ValueError as e:
            raise WindowError("dates must be YYYY-MM-DD (IST)") from e
        if d1 < d0:
            raise WindowError("'to' is before 'from'")
        lo, hi = ist_day_bounds(d0)[0], ist_day_bounds(d1)[1]
        if hi - lo > MAX_RANGE + timedelta(days=1):
            raise WindowError("a custom range can be at most 90 days")
        return {"preset": "custom", "from": lo, "to": min(hi, now), "label": f"{d0:%d %b} – {d1:%d %b %Y}"}
    window = window or "24h"
    if window == "since_last":
        lo = last_checked or now - PRESETS["24h"]
        lo = max(lo, now - MAX_RANGE)
        label = f"Since you last checked ({to_ist(lo):%d %b, %H:%M} IST)" if last_checked else "Last 24 hours (first visit)"
        return {"preset": "since_last", "from": lo, "to": now, "label": label}
    if window not in PRESETS:
        raise WindowError(f"unknown window '{window}'")
    return {"preset": window, "from": now - PRESETS[window], "to": now,
            "label": {"24h": "Last 24 hours", "3d": "Last 3 days", "7d": "Last 7 days", "30d": "Last 30 days"}[window]}


def _card(f: Filing, e: Enrichment, c: Company) -> dict:
    return {
        "id": f.id, "symbol": c.nse_symbol, "company": c.name, "filed_at": to_ist(f.filed_at).isoformat(),
        "type": e.type, "type_source": e.type_source, "bucket": e.bucket, "score": e.score,
        "headline": e.headline or f.subject, "one_liner": e.one_liner, "why": e.score_breakdown,
        "thesis_impact": e.thesis_impact, "why_it_matters": e.why_it_matters, "payload": e.payload,
        "confidence": e.confidence, "pdf_url": f.pdf_url, "category": f.category_raw, "subcategory": f.subcategory_raw,
        "reviewed": e.reviewed_at is not None, "revised": f.supersedes_id is not None, "failed": f.status == "failed",
        "error": f.error if f.status == "failed" else None,
    }


def digest(window: str | None = "24h", start: str | None = None, end: str | None = None, as_of: str | None = None,
           include_peers: bool = False, min_material: int = 0) -> dict:
    t0 = time.perf_counter()
    as_of_dt = None
    if as_of:
        try:
            as_of_dt = datetime.fromisoformat(as_of)
        except ValueError as e:
            raise WindowError("as_of must be an ISO date or datetime") from e
        if as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=IST) if "T" in as_of else ist_day_bounds(as_of_dt.date())[1]
    last_checked = _last_checked() if window == "since_last" and not (start or end) else None
    w = resolve_window(window, start, end, now=as_of_dt, last_checked=last_checked)
    if w["preset"] in PRESETS and not as_of:   # rolling windows: one entry each, kept fresh by the warmer
        key = ("preset", None, include_peers, w["preset"])
    else:
        key = (round(w["from"].timestamp() // 60), round(w["to"].timestamp() // 60), include_peers, w["preset"])
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            out = dict(hit[1])
            out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            out["cached"] = True
            return out
    def query(s):
        from sqlalchemy.orm import defer
        q = (select(Filing, Enrichment, Company)
             .options(defer(Filing.raw), defer(Filing.body), defer(Company.bse_snapshot), defer(Company.note))
             .join(Enrichment, Enrichment.filing_id == Filing.id).join(Company, Company.id == Filing.company_id)
             .where(Filing.filed_at >= w["from"], Filing.filed_at <= w["to"], Filing.status != "duplicate")
             .order_by(Enrichment.score.desc(), Filing.filed_at.desc()))
        if not include_peers:
            q = q.where(Company.is_portfolio.is_(True))
        return [_card(f, e, c) for f, e, c in s.execute(q).all()]

    cards = read_session_run(query)
    groups = {b: [c for c in cards if c["bucket"] == b] for b in ("attention", "worth_a_look", "routine")}
    out = {
        "window": {"preset": w["preset"], "label": w["label"], "from": to_ist(w["from"]).isoformat(),
                   "to": to_ist(w["to"]).isoformat(), "as_of": as_of},
        "counts": {b: len(v) for b, v in groups.items()},
        "attention": groups["attention"], "worth_a_look": groups["worth_a_look"],
        # Routine is collapsed: the client shows one row with the count and can expand the minimal list.
        "routine": [{"id": c["id"], "symbol": c["symbol"], "headline": c["headline"], "type": c["type"],
                     "filed_at": c["filed_at"], "pdf_url": c["pdf_url"]} for c in groups["routine"]],
        "unreviewed": {b: sum(1 for c in groups[b] if not c["reviewed"]) for b in ("attention", "worth_a_look")},
        "caught_up": all(c["reviewed"] for c in groups["attention"] + groups["worth_a_look"]),
        "cached": False,
    }
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    with _lock:
        _cache[key] = (time.monotonic(), out)
    return out


def _last_checked():
    def get(s):
        st = s.get(UserState, 1)
        return st.last_checked_at if st else None
    return read_session_run(get)


_warmer_started = False


def start_warmer(interval: float = 50.0):
    """Keep the standard windows precomputed in this process, so a request almost never pays for a query
    (the feed's p95 target is 300 ms and one Neon round trip from India is ~85 ms)."""
    global _warmer_started
    if _warmer_started:
        return
    _warmer_started = True

    def loop():
        import logging
        while True:
            for w in ("24h", "3d", "7d", "30d"):
                try:
                    with _lock:
                        for k in [k for k in _cache if k[3] == w and k[0] == "preset"]:
                            _cache.pop(k, None)
                    digest(w)
                except Exception as e:  # noqa: BLE001 - a failed warm-up just means the next request queries
                    logging.getLogger("finn.digest").warning("warm-up failed for %s: %s", w, e)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="digest-warmer").start()


def mark_checked(at: datetime | None = None) -> str:
    at = at or datetime.now(timezone.utc)
    with session() as s:
        st = s.get(UserState, 1)
        if st is None:
            st = UserState(id=1, prefs={})
            s.add(st)
        st.last_checked_at = at
    invalidate()
    return to_ist(at).isoformat()


def mark_reviewed(filing_id: int, undo: bool = False) -> bool:
    with session() as s:
        e = s.get(Enrichment, filing_id)
        if not e:
            return False
        e.reviewed_at = None if undo else datetime.now(timezone.utc)
    invalidate()
    return True
