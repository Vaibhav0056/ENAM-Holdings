"""BSE corporate announcements via the `bse` library (probed: docs/probes/bse.json).

announcements(scripcode, from_date, to_date, page_no) -> {"Table": rows, "Table1": [{"ROWCNT": n}]}.
DissemDT is IST wall-clock time without a zone; it is stored as UTC.
content_hash = sha256(SCRIP_CD|NEWSID|NEWSSUB|ATTACHMENTNAME): the same filing fetched twice never duplicates, while
a revised filing (new NEWSID / attachment) is a new row that is linked to the one it supersedes.
"""
from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from ..database import session
from ..fiscal import to_utc
from ..models import Filing
from .common import insert_ignore, log_run

log = logging.getLogger("finn.sources.bse")
PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
REVISION_RE = re.compile(r"\b(revised|corrigendum|addendum|rectification|correction|erratum)\b", re.I)


def content_hash(row: dict) -> str:
    key = f"{row.get('SCRIP_CD')}|{row.get('NEWSID')}|{(row.get('NEWSSUB') or '').strip()}|{(row.get('ATTACHMENTNAME') or '').strip()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def filed_at(row: dict) -> datetime | None:
    raw = row.get("DissemDT") or row.get("NEWS_DT") or row.get("DT_TM")
    if not raw:
        return None
    return to_utc(datetime.fromisoformat(str(raw).split(".")[0]))


def to_filing(row: dict, company_id: int) -> dict | None:
    when = filed_at(row)
    if when is None:
        return None
    att = (row.get("ATTACHMENTNAME") or "").strip()
    subject = (row.get("NEWSSUB") or row.get("HEADLINE") or "").strip()
    return {
        "company_id": company_id, "exchange": "BSE", "exchange_id": str(row.get("NEWSID") or ""), "filed_at": when,
        "category_raw": (row.get("CATEGORYNAME") or "").strip() or None,
        "subcategory_raw": (row.get("SUBCATNAME") or "").strip() or None,
        "subject": subject, "body": (row.get("MORE") or row.get("HEADLINE") or "").strip() or None,
        "pdf_url": PDF_BASE + att if att else None, "xbrl_url": None,
        "content_hash": content_hash(row), "status": "new",
        "raw": {k: v for k, v in row.items() if k in (
            "NEWSID", "SCRIP_CD", "NEWSSUB", "HEADLINE", "CATEGORYNAME", "SUBCATNAME", "DissemDT", "ATTACHMENTNAME",
            "XML_NAME", "CRITICALNEWS", "QUARTER_ID", "Fld_Attachsize", "AUDIO_VIDEO_FILE")},
    }


def fetch_all(b, code: str, days: int) -> list[dict]:
    """Pages until ROWCNT rows are collected (one page held 43 TCS rows for 90 days in the probe)."""
    end = datetime.now()
    start = end - timedelta(days=days)
    out, page = [], 1
    while True:
        res = _retry(lambda: b.announcements(page_no=page, scripcode=code, from_date=start, to_date=end))
        rows = res.get("Table", []) or []
        out.extend(rows)
        total = (res.get("Table1") or [{}])[0].get("ROWCNT", len(out))
        if not rows or len(out) >= total or page >= 30:
            return out
        page += 1
        time.sleep(0.5)


def _retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except (TimeoutError, ConnectionError):
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))


def link_revisions(company_id: int) -> int:
    """A filing whose subject says revised/corrigendum supersedes the latest earlier filing of the same company
    with the same sub-category within 7 days. Returns how many links were made."""
    n = 0
    with session() as s:
        rows = s.scalars(select(Filing).where(Filing.company_id == company_id).order_by(Filing.filed_at)).all()
        for f in rows:
            if f.supersedes_id or not REVISION_RE.search(f.subject or ""):
                continue
            prev = [p for p in rows if p.id != f.id and p.filed_at < f.filed_at
                    and (f.filed_at - p.filed_at) <= timedelta(days=7)
                    and p.subcategory_raw == f.subcategory_raw and not REVISION_RE.search(p.subject or "")]
            if prev:
                f.supersedes_id = prev[-1].id
                n += 1
    return n


def ingest(companies, days: int = 90) -> dict[str, dict]:
    from bse import BSE
    out = {}
    with BSE(download_folder=tempfile.gettempdir()) as b:
        for c in companies:
            if not c.bse_code:
                out[c.nse_symbol] = {"seen": 0, "added": 0, "error": "no BSE code"}
                continue
            try:
                raw = fetch_all(b, c.bse_code, days)
                rows = [r for r in (to_filing(x, c.id) for x in raw) if r]
                added = insert_ignore(Filing, rows, ["content_hash"])
                revised = link_revisions(c.id)
                out[c.nse_symbol] = {"seen": len(rows), "added": added, "revisions_linked": revised}
                log_run("bse_filings", c.id, added, len(rows))
            except Exception as e:  # noqa: BLE001
                log.warning("bse failed %s: %s", c.nse_symbol, e)
                out[c.nse_symbol] = {"seen": 0, "added": 0, "error": str(e)[:200]}
                log_run("bse_filings", c.id, 0, 0, ok=False, error=str(e))
            time.sleep(0.5)
    return out
