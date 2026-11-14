"""Source chains with per-source circuit breakers.

    announcements: BSE -> NSE API -> NSE RSS        (a source only gets the companies the previous one failed for)
    prices:        yfinance -> NSE bhavcopy
    events:        BSE -> NSE event calendar, plus events derived from stored filings (always)

Every row is validated before it is written; writes are idempotent inserts (ON CONFLICT DO NOTHING / DO UPDATE for
prices), one transaction per source, and nothing is ever deleted. A source that fails BREAKER_FAILS runs in a row
is skipped for BREAKER_OPEN, then tried again (half-open). State lives in the source_health table, so the local
scheduler, GitHub Actions and POST /tasks/ingest all see the same breakers.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from rapidfuzz import fuzz
from sqlalchemy import select

from ..database import session
from ..models import CalendarEvent, Document, Filing, Price, SourceHealth
from .common import PoliteHTTP, insert_ignore, log_run

log = logging.getLogger("finn.sources.chain")
BREAKER_FAILS = 3
BREAKER_OPEN = timedelta(hours=3)
DUP_WINDOW = timedelta(minutes=20)
KIND = {"bse_announcements": "announcements", "nse_api_announcements": "announcements", "nse_rss_announcements": "announcements",
        "yfinance_prices": "prices", "nse_bhavcopy_prices": "prices",
        "bse_events": "events", "nse_events": "events", "derived_events": "events"}
_http = None


def http() -> PoliteHTTP:
    global _http
    if _http is None:
        _http = PoliteHTTP(delay=1.5)
    return _http


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """Stored times are UTC; SQLite (tests) hands them back without a zone."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


# ---------- circuit breaker ----------

def breaker_open(source: str, at: datetime | None = None) -> datetime | None:
    """The time the breaker reopens, if it is open now."""
    at = at or now()
    with session() as s:
        h = s.get(SourceHealth, source)
        until = aware(h.open_until) if h else None
        return until if until and until > at else None


def record(source: str, ok: bool, added: int = 0, seen: int = 0, error: str | None = None, at: datetime | None = None) -> None:
    at = at or now()
    with session() as s:
        h = s.get(SourceHealth, source) or SourceHealth(source=source, kind=KIND.get(source, "other"),
                                                        consecutive_failures=0, last_added=0, last_seen=0, total_added=0)
        s.add(h)
        if ok:
            h.consecutive_failures, h.open_until, h.last_success_at = 0, None, at
            h.last_added, h.last_seen, h.total_added = added, seen, (h.total_added or 0) + added
        else:
            h.consecutive_failures = (h.consecutive_failures or 0) + 1
            h.last_error_at, h.last_error = at, (error or "failed")[:1000]
            if h.consecutive_failures >= BREAKER_FAILS:
                h.open_until = at + BREAKER_OPEN
    log_run(source, None, added if ok else 0, seen, ok=ok, error=error)


# ---------- validation (nothing unvalidated is written) ----------

def valid_filing(r: dict, company_ids: set[int], at: datetime) -> bool:
    w = r.get("filed_at")
    return (r.get("company_id") in company_ids and bool((r.get("subject") or "").strip()) and isinstance(w, datetime)
            and w.tzinfo is not None and at - timedelta(days=400) <= w <= at + timedelta(days=1)
            and (r.get("pdf_url") is None or str(r["pdf_url"]).startswith("https://")) and len(r.get("content_hash") or "") == 64)


def valid_price(r: dict) -> bool:
    c = r.get("close")
    ok = isinstance(c, (int, float)) and math.isfinite(c) and c > 0 and isinstance(r.get("date"), date) and r["date"] <= date.today()
    if ok and r.get("high") is not None and r.get("low") is not None:
        ok = r["high"] >= r["low"] > 0
    return ok


def valid_event(r: dict, company_ids: set[int]) -> bool:
    d = r.get("event_date")
    return (r.get("company_id") in company_ids and isinstance(d, date) and bool((r.get("title") or "").strip())
            and abs((d - date.today()).days) <= 400)


# ---------- writers ----------

# Disclosure topics in either exchange's wording. BSE: "Announcement under Regulation 30 (LODR)-Change in
# Directorate"; NSE: "Change in Director(s)" / "HDFC Bank Limited has informed the Exchange about ...".
TOPICS = {
    "people": r"director|management|managerial|kmp|\bceo\b|\bcfo\b|company secretary|appointment|cessation|resign",
    "analyst_meet": r"analyst|investor meet|institutional investor|con\.? ?call|conference call|earnings call",
    "allotment": r"\besop|\besos|\besps|allotment",
    "newspaper": r"newspaper",
    "shareholder_meeting": r"shareholder|postal ballot|\bagm\b|\begm\b|general meeting|voting results|scrutini",
    "results": r"financial results|\bresults\b",
    "board_meeting": r"board meeting|outcome of",
    "rating": r"credit rating|\brating\b",
    "trading_window": r"trading window",
    "certificate": r"certificate|regulation 74|depositor",
    "dividend": r"dividend|record date",
    "acquisition": r"acquisition|amalgamation|merger|scheme of arrangement",
}


def topics(text: str) -> set[str]:
    import re
    return {k for k, rx in TOPICS.items() if re.search(rx, text or "", re.I)}


def _text(f) -> str:
    g = f.get if isinstance(f, dict) else lambda k: getattr(f, k)
    return f"{g('subject') or ''} {g('body') or ''}"[:500].lower()


def mark_cross_exchange(rows: list[dict]) -> int:
    """A row that repeats a disclosure already stored from the other exchange (same company, within 20 minutes, and
    the same disclosure topic, near-identical text or the same PDF file name) is stored with status 'duplicate',
    never shown, never deleted. Exact PDF-byte matches are linked later by link_pdf_duplicates()."""
    by_co = defaultdict(list)
    for r in rows:
        by_co[r["company_id"]].append(r)
    n = 0
    with session() as s:
        for cid, rs in by_co.items():
            lo, hi = min(r["filed_at"] for r in rs) - DUP_WINDOW, max(r["filed_at"] for r in rs) + DUP_WINDOW
            existing = s.execute(select(Filing.id, Filing.exchange, Filing.filed_at, Filing.subject, Filing.body, Filing.pdf_url)
                                 .where(Filing.company_id == cid, Filing.filed_at.between(lo, hi), Filing.status != "duplicate")).all()
            for r in rs:
                for e in existing:
                    if e.exchange == r["exchange"] or abs(aware(e.filed_at) - r["filed_at"]) > DUP_WINDOW:
                        continue
                    same_file = r.get("pdf_url") and e.pdf_url and r["pdf_url"].rsplit("/", 1)[-1] == e.pdf_url.rsplit("/", 1)[-1]
                    etext = f"{e.subject or ''} {e.body or ''}"[:500].lower()
                    same_topic = bool(topics(_text(r)) & topics(etext))
                    if same_file or same_topic or fuzz.token_set_ratio(_text(r), etext) >= 70:
                        r["status"], r["error"] = "duplicate", f"same disclosure as filing {e.id} ({e.exchange})"
                        n += 1
                        break
    return n


def write_filings(rows: list[dict]) -> int:
    from .bse_filings import link_revisions
    for r in rows:
        r.setdefault("error", None)        # multi-row inserts need identical keys
    mark_cross_exchange(rows)
    added = insert_ignore(Filing, rows, ["content_hash"])
    for cid in {r["company_id"] for r in rows}:
        link_revisions(cid)
    return added


def link_pdf_duplicates() -> int:
    """Exact content hash: two filings (BSE and NSE) whose PDFs have identical bytes are one disclosure; the later
    row is marked duplicate. Uses documents.sha256 from the PDF store, so it applies once PDFs are fetched."""
    n = 0
    with session() as s:
        for d in s.scalars(select(Document).where(Document.kind == "filing", Document.status == "duplicate")):
            try:
                orig = s.get(Document, int((d.error or "").rsplit(" ", 1)[-1]))
            except ValueError:
                continue
            if not orig:
                continue
            a = s.scalar(select(Filing).where(Filing.company_id == d.company_id, Filing.pdf_url == d.url))
            b = s.scalar(select(Filing).where(Filing.company_id == orig.company_id, Filing.pdf_url == orig.url))
            if a and b and a.id != b.id and a.exchange != b.exchange and "duplicate" not in (a.status, b.status):
                later = a if a.id > b.id else b
                later.status, later.error = "duplicate", f"same PDF bytes as filing {(b if later is a else a).id}"
                n += 1
    return n


def write_prices(rows: list[dict]) -> int:
    return insert_ignore(Price, rows, ["symbol", "date"], update_cols=["open", "high", "low", "close", "adj_close", "volume"])


def write_events(rows: list[dict]) -> int:
    seen, unique = set(), []
    for r in rows:
        k = (r["company_id"], r["kind"], r["event_date"], r["title"])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    return insert_ignore(CalendarEvent, unique, ["company_id", "kind", "event_date", "title"])


# ---------- the chains ----------

def run_chain(kind: str, steps, companies, write, validate) -> dict:
    """steps: [(source name, fetch(companies) -> (rows, failed companies))]. Each source gets only what the previous
    ones could not deliver. A source counts as failed (for its breaker) when it raises or fails half its companies."""
    remaining, report = list(companies), {}
    for name, fetch in steps:
        if not remaining:
            report[name] = {"skipped": "nothing left to fetch"}
            continue
        until = breaker_open(name)
        if until:
            report[name] = {"skipped": f"circuit open until {until.isoformat(timespec='minutes')}"}
            continue
        try:
            rows, failed = fetch(remaining)
        except Exception as e:  # noqa: BLE001 - the next source takes over
            record(name, False, error=f"{type(e).__name__}: {e}")
            report[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            continue
        good = [r for r in rows if validate(r)]
        added = write(good) if good else 0
        bad_share = len(failed) / max(1, len(remaining))
        ok = bad_share < 0.5
        record(name, ok, added, len(good), error=None if ok else f"{len(failed)} of {len(remaining)} companies failed")
        report[name] = {"companies": len(remaining), "rows": len(good), "rejected": len(rows) - len(good),
                        "added": added, "failed_companies": [c.nse_symbol for c in failed][:30]}
        remaining = failed
    report["unserved"] = [c.nse_symbol for c in remaining]
    return report


def announcements(companies, days: int = 7) -> dict:
    from . import bse_filings, nse
    ids, at = {c.id for c in companies}, now()
    return run_chain("announcements", [
        ("bse_announcements", lambda cs: bse_filings.fetch(cs, days)),
        ("nse_api_announcements", lambda cs: nse.api_announcements(http(), cs, days)),
        ("nse_rss_announcements", lambda cs: nse.rss_announcements(http(), cs, days)),
    ], companies, write_filings, lambda r: valid_filing(r, ids, at))


def _yf(companies, days):
    from . import prices
    rows, failed = [], []
    for c in companies:
        try:
            got = prices.frame_to_rows(prices.fetch(c.yahoo_symbol, days), c.yahoo_symbol, c.id)
            if not got:
                raise ValueError("no bars")
            rows += got
        except Exception:  # noqa: BLE001
            failed.append(c)
    try:                      # the market index, for the replay's market-relative moves (not a company: never "failed")
        rows += prices.frame_to_rows(prices.fetch(prices.MARKET_INDEX, days), prices.MARKET_INDEX, None)
    except Exception:  # noqa: BLE001
        pass
    return rows, failed


def price_chain(companies, days: int = 10) -> dict:
    from . import nse
    return run_chain("prices", [
        ("yfinance_prices", lambda cs: _yf(cs, days)),
        ("nse_bhavcopy_prices", lambda cs: nse.bhavcopy_prices(http(), cs, min(days, 10))),
    ], companies, write_prices, valid_price)


def event_chain(companies) -> dict:
    """Exchange calendars first; events derived from stored board-meeting intimations always run as well, so the
    week-ahead strip has whatever the filings themselves announce even when both exchanges are down."""
    from . import calendar, nse
    ids = {c.id for c in companies}
    report = run_chain("events", [
        ("bse_events", lambda cs: (calendar.bse_events(cs), [])),
        ("nse_events", lambda cs: (nse.events(http(), cs), [])),
    ], companies, write_events, lambda r: valid_event(r, ids))
    try:
        rows = [r for r in calendar.derived_events(companies) if valid_event(r, ids)]
        added = write_events(rows)
        record("derived_events", True, added, len(rows))
        report["derived_events"] = {"rows": len(rows), "added": added}
    except Exception as e:  # noqa: BLE001
        record("derived_events", False, error=str(e))
        report["derived_events"] = {"error": str(e)[:200]}
    return report


def health() -> list[dict]:
    with session() as s:
        def iso(x):
            return aware(x).isoformat(timespec="seconds") if x else None
        return [{"source": h.source, "kind": h.kind, "ok": h.consecutive_failures == 0,
                 "consecutive_failures": h.consecutive_failures,
                 "circuit_open_until": iso(h.open_until) if h.open_until and aware(h.open_until) > now() else None,
                 "last_success_at": iso(h.last_success_at), "last_error_at": iso(h.last_error_at), "last_error": h.last_error,
                 "rows_added_last_run": h.last_added, "rows_seen_last_run": h.last_seen, "rows_added_total": h.total_added}
                for h in s.scalars(select(SourceHealth).where(SourceHealth.kind != "lock")
                                  .order_by(SourceHealth.kind, SourceHealth.source))]
