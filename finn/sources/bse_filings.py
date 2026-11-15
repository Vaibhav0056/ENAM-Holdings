"""BSE corporate announcements via the `bse` library.

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


def fetch_all(b, code: str, days: int, category: str = "-1") -> list[dict]:
    """Pages until ROWCNT rows are collected (one page held 43 TCS rows for 90 days in the probe). BSE answers
    400 for a date range much over a year, so callers keep `days` <= 365."""
    end = datetime.now()
    start = end - timedelta(days=min(days, 365))
    out, page = [], 1
    while True:
        res = _retry(lambda: b.announcements(page_no=page, scripcode=code, from_date=start, to_date=end, category=category))
        rows = res.get("Table", []) or []
        out.extend(rows)
        total = (res.get("Table1") or [{}])[0].get("ROWCNT", len(out))
        if not rows or len(out) >= total or page >= 30:
            return out
        page += 1
        time.sleep(1.0)


def _retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except (TimeoutError, ConnectionError) as e:
            if i == attempts - 1 or _refused(e):
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


SPACING = 1.5          # seconds between companies: BSE refused FINN for days after a fast backfill (Nov 2026)
last_refusal: dict = {}


def _refused(e: Exception) -> bool:
    t = str(e)
    return any(x in t for x in ("403", "429", "Forbidden", "Too Many"))


def _record_refusal(r, *_, **__):
    """Response hook: keep the exact refusal (status, headers, body) for /health/data and the build log."""
    if r.status_code >= 400:
        last_refusal.update(status=r.status_code, url=r.url[:160], at=datetime.now().isoformat(timespec="seconds"),
                            headers={k: r.headers.get(k) for k in ("Server", "Content-Type", "Retry-After",
                                                                   "Akamai-GRN", "X-Akamai-Request-ID") if r.headers.get(k)},
                            body=r.text[:300])
        log.warning("BSE refused: %s", last_refusal)


def fetch(companies, days: int, category: str = "-1") -> tuple[list[dict], list]:
    """Announcements for each company as filing rows. Returns (rows, companies that failed). Exponential backoff on
    403/429 (2 s, 4 s); after 3 companies in a row are refused, stops asking BSE this run and hands the rest on."""
    from bse import BSE
    rows, failed, refused_run = [], [], 0
    with BSE(download_folder=tempfile.gettempdir()) as b:
        b.session.hooks["response"].append(_record_refusal)
        for i, c in enumerate(companies):
            if refused_run >= 3:
                failed += list(companies[i:])
                break
            if not c.bse_code:
                failed.append(c)
                continue
            for attempt in range(3):
                try:
                    raw = fetch_all(b, c.bse_code, days, category)
                    rows += [r for r in (to_filing(x, c.id) for x in raw) if r]
                    refused_run = 0
                    break
                except Exception as e:  # noqa: BLE001
                    if _refused(e) and attempt < 2:
                        time.sleep(2 * 2 ** attempt)
                        continue
                    log.warning("bse failed %s: %s", c.nse_symbol, e)
                    refused_run = refused_run + 1 if _refused(e) else 0
                    failed.append(c)
                    break
            time.sleep(SPACING)
    return rows, failed
