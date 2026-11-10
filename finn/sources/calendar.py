"""Catalysts calendar from BSE (probed: docs/probes/bse.json).

- resultCalendar(from, to) -> scrip_Code, meeting_date "11 Nov 2026": the board meeting that approves results.
- actions(from, to) -> scrip_code, Purpose, Ex_date, RD_Date: dividends, bonus, splits, buybacks, AGM book closures.
- Board-meeting intimations in the filings table ("... to consider ... on 14th November 2026"): other board meetings.
Both BSE calls return every listed company, so rows are filtered to ours by scrip code.
"""
from __future__ import annotations

import logging
import re
import tempfile
from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..database import session
from ..models import CalendarEvent, Filing
from .common import insert_ignore, log_run

log = logging.getLogger("finn.sources.calendar")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


def parse_bse_date(s: str | None) -> date | None:
    if not s or not str(s).strip():
        return None
    s = str(s).strip()
    for fmt in ("%d %b %Y", "%Y%m%d", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def action_kind(purpose: str) -> str:
    p = purpose.lower()
    if "agm" in p or "annual general meeting" in p:
        return "agm"
    if "egm" in p or "extra ordinary" in p or "extraordinary" in p:
        return "egm"
    return "ex_date"


def action_title(purpose: str) -> str:
    p = re.sub(r"\s+", " ", purpose).strip()
    m = re.match(r"(.*?dividend)\s*-\s*rs\.?\s*-\s*([\d.]+)", p, re.I)
    if m:
        return f"{m.group(1).strip()} ₹{float(m.group(2)):g}/share"
    return p


_MEETING_DATE_RE = re.compile(
    rf"(?:on|dated|held on|scheduled (?:on|for))\s*(?:\w+day,?\s*)?(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTHS})[a-z]*,?\s*(\d{{4}})",
    re.I)
_MEETING_DATE_RE2 = re.compile(r"(?:on|dated)\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", re.I)


def meeting_date_from_text(text: str) -> date | None:
    m = _MEETING_DATE_RE.search(text or "")
    if m:
        return datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", "%d %b %Y").date()
    m = _MEETING_DATE_RE2.search(text or "")
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def board_meetings_from_filings(by_id: dict[int, object], since: date) -> list[dict]:
    rows = []
    with session() as s:
        q = select(Filing).where(Filing.category_raw == "Board Meeting")
        for f in s.scalars(q):
            if f.company_id not in by_id or not re.search(r"intimation|to consider|prior", f.subject or "", re.I):
                continue
            d = meeting_date_from_text(f"{f.subject} {f.body or ''}")
            if d and d >= since:
                purpose = "results" if re.search(r"result", f"{f.subject} {f.body}", re.I) else "board_meeting"
                rows.append({"company_id": f.company_id, "kind": purpose, "event_date": d,
                             "title": "Board meeting: results" if purpose == "results" else "Board meeting",
                             "source": f"bse_filing:{f.id}"})
    return rows


def ingest(companies, back_days: int = 90, ahead_days: int = 30) -> dict:
    from bse import BSE
    by_code = {str(c.bse_code): c for c in companies if c.bse_code}
    by_id = {c.id: c for c in companies}
    today = date.today()
    start, end = datetime.now() - timedelta(days=back_days), datetime.now() + timedelta(days=ahead_days)
    rows, errors = [], []
    with BSE(download_folder=tempfile.gettempdir()) as b:
        try:
            for r in b.resultCalendar(from_date=start, to_date=end):
                c = by_code.get(str(r.get("scrip_Code")))
                d = parse_bse_date(r.get("meeting_date"))
                if c and d:
                    rows.append({"company_id": c.id, "kind": "results", "event_date": d,
                                 "title": "Board meeting: results", "source": "bse_result_calendar"})
        except Exception as e:  # noqa: BLE001
            errors.append(f"resultCalendar: {e}")
        for by in ("ex",):
            try:
                for a in b.actions(from_date=start, to_date=end, by_date=by):
                    c = by_code.get(str(a.get("scrip_code")))
                    purpose = (a.get("Purpose") or "").strip()
                    d = parse_bse_date(a.get("Ex_date")) or parse_bse_date(a.get("exdate")) or parse_bse_date(a.get("RD_Date"))
                    if c and d and purpose:
                        rows.append({"company_id": c.id, "kind": action_kind(purpose), "event_date": d,
                                     "title": action_title(purpose)[:300], "source": "bse_actions"})
                        rd = parse_bse_date(a.get("RD_Date"))
                        if rd and rd != d:
                            rows.append({"company_id": c.id, "kind": "record_date", "event_date": rd,
                                         "title": f"Record date: {action_title(purpose)}"[:300], "source": "bse_actions"})
            except Exception as e:  # noqa: BLE001
                errors.append(f"actions: {e}")
    rows += board_meetings_from_filings(by_id, today - timedelta(days=back_days))
    seen, unique = set(), []
    for r in rows:
        k = (r["company_id"], r["kind"], r["event_date"], r["title"])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    added = insert_ignore(CalendarEvent, unique, ["company_id", "kind", "event_date", "title"])
    log_run("calendar", None, added, len(unique), ok=not errors, error="; ".join(errors) or None)
    upcoming = sum(1 for r in unique if today <= r["event_date"] <= today + timedelta(days=7))
    return {"seen": len(unique), "added": added, "next_7_days": upcoming, "errors": errors}
