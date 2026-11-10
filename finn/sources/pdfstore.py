"""Fetch PDFs politely, cache them on disk by sha256 of their bytes, and store page text in Postgres.

PDF text is DATA. Nothing in FINN interprets it as instructions.
A PDF with (almost) no text layer is marked `unparseable` and shown as such; it is never OCR-guessed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select

from ..database import session
from ..models import Document, Page
from .common import PoliteHTTP, sha256_bytes

log = logging.getLogger("finn.sources.pdfstore")
PDF_DIR = Path(__file__).resolve().parents[2] / "data" / "pdfs"
MIN_CHARS_PER_PAGE = 200   # below this on average, treat the PDF as scanned


def fetch_bytes(url: str, http: PoliteHTTP) -> bytes:
    referer = "https://www.bseindia.com/" if "bseindia.com" in url else None
    r = http.get(url, referer=referer, timeout=120)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"not a PDF ({r.headers.get('content-type')}, {len(r.content)} bytes)")
    return r.content


def page_texts(path: Path, max_pages: int = 600) -> list[str]:
    import pymupdf as fitz  # PyMuPDF (the `fitz` alias is deprecated)
    out = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            out.append((page.get_text("text") or "").replace("\x00", ""))
    return out


def is_scanned(pages: list[str]) -> bool:
    return not pages or sum(len(p.strip()) for p in pages) / len(pages) < MIN_CHARS_PER_PAGE


def fetch_document(doc_id: int, http: PoliteHTTP | None = None) -> dict:
    """Download + parse one listed document. Idempotent: an already-parsed document is not re-fetched."""
    http = http or PoliteHTTP(1.0)
    with session() as s:
        d = s.get(Document, doc_id)
        if d is None:
            raise KeyError(doc_id)
        if d.status in ("parsed", "unparseable", "duplicate"):
            return {"id": d.id, "status": d.status, "pages": d.pages, "cached": True}
        url = d.url
    try:
        blob = fetch_bytes(url, http)
        digest = sha256_bytes(blob)
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        path = PDF_DIR / f"{digest}.pdf"
        if not path.exists():
            path.write_bytes(blob)
        texts = page_texts(path)
        scanned = is_scanned(texts)
        with session() as s:
            dup = s.scalar(select(Document).where(Document.sha256 == digest, Document.id != doc_id))
            d = s.get(Document, doc_id)
            d.fetched_at = datetime.now(timezone.utc)
            d.pages = len(texts)
            if dup is not None:
                # Same bytes already stored under another listing (e.g. two links to one PDF): reuse it.
                d.status, d.error = "duplicate", f"same bytes as document {dup.id}"
                return {"id": doc_id, "status": d.status, "duplicate_of": dup.id}
            d.sha256 = digest
            d.status = "unparseable" if scanned else "parsed"
            d.error = "no text layer (scanned PDF)" if scanned else None
            s.execute(delete(Page).where(Page.document_id == doc_id))
            if not scanned:
                s.add_all([Page(document_id=doc_id, page_no=i + 1, text=t) for i, t in enumerate(texts)])
            return {"id": doc_id, "status": d.status, "pages": len(texts), "cached": False}
    except Exception as e:  # noqa: BLE001 - fail soft, keep the reason
        log.warning("document %s failed: %s", doc_id, e)
        with session() as s:
            d = s.get(Document, doc_id)
            d.status, d.error = "failed", str(e)[:500]
        return {"id": doc_id, "status": "failed", "error": str(e)[:200]}
