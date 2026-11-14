"""A filing's PDF as pages: fetched once through the shared PDF store (documents.kind = 'filing'), cached by sha256,
page text in Postgres so cards, evidence links and the copilot can cite it."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from ..database import session
from ..models import Document, Filing, Page
from ..sources.common import PoliteHTTP, insert_ignore
from ..sources.pdfstore import PDF_DIR, fetch_document

_http = None


def http() -> PoliteHTTP:
    global _http
    if _http is None:
        _http = PoliteHTTP(1.0)
    return _http


class NoPDF(RuntimeError):
    pass


def filing_document(filing_id: int) -> tuple[int, str]:
    """(document id, status) for the filing's PDF, fetching it if needed."""
    with session() as s:
        f = s.get(Filing, filing_id)
        if not f.pdf_url:
            raise NoPDF("filing has no attachment")
        cid, url, title = f.company_id, f.pdf_url, f.subject[:120]
    insert_ignore(Document, [{"company_id": cid, "kind": "filing", "title": title, "url": url, "status": "listed"}],
                  ["company_id", "url"])
    with session() as s:
        doc_id = s.scalar(select(Document.id).where(Document.company_id == cid, Document.url == url))
    res = fetch_document(doc_id, http())
    return doc_id, res["status"]


def pages(doc_id: int) -> list[str]:
    """Page texts. A document stored as a duplicate (same bytes as another) reads the original's pages."""
    with session() as s:
        d = s.get(Document, doc_id)
        if d and d.status == "duplicate" and d.error:
            try:
                doc_id = int(d.error.rsplit(" ", 1)[-1])
            except ValueError:
                pass
        rows = s.execute(select(Page.page_no, Page.text).where(Page.document_id == doc_id).order_by(Page.page_no)).all()
    return [t for _, t in rows]


def pdf_path(doc_id: int) -> Path | None:
    with session() as s:
        d = s.get(Document, doc_id)
        sha = d.sha256 if d else None
        if d and d.status == "duplicate" and d.error:
            other = int(d.error.rsplit(" ", 1)[-1])
            sha = s.get(Document, other).sha256
    p = PDF_DIR / f"{sha}.pdf" if sha else None
    return p if p and p.exists() else None


def layout_pages(path: Path, fast_pages: list[str], wanted: list[int]) -> list[str]:
    """Every page of the document: PyMuPDF text (fast, all pages), with pdfplumber's layout text (keeps a table row's
    label and its numbers on one line) substituted for the pages in `wanted` (1-indexed). Searching all pages with
    the fast text and paying for layout only where the P&L is lets long filings be read end to end."""
    import pdfplumber
    out = list(fast_pages)
    with pdfplumber.open(path) as pdf:
        for n in sorted(set(wanted)):
            if 1 <= n <= len(pdf.pages) and n <= len(out):
                try:
                    out[n - 1] = pdf.pages[n - 1].extract_text() or out[n - 1]
                except Exception:  # noqa: BLE001 - keep the fast text for a page pdfplumber cannot read
                    pass
    return out


def plumber_pages(path: Path, max_pages: int = 40) -> list[str]:
    """pdfplumber keeps table rows on one line better than PyMuPDF, which matters for results statements."""
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages):
            if i >= max_pages:
                break
            try:
                out.append(p.extract_text() or "")
            except Exception:  # noqa: BLE001 - a bad page is an empty page
                out.append("")
    return out
