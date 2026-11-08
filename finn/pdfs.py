"""PDF download + cache (by sha256 of URL) and page-wise text extraction.

PDF text is DATA. It is never interpreted as instructions anywhere in FINN.
"""
import hashlib, logging, time
from pathlib import Path
import requests
from . import db

log = logging.getLogger("finn.pdfs")
PDF_DIR = db.DATA / "pdfs"
UA = "Mozilla/5.0 (compatible; FINN-research-app/0.1)"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def download(url: str, referer: str = "https://www.bseindia.com/", symbol: str = "", kind: str = "",
             title: str = "", polite_sleep: float = 0.0) -> Path:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    h = sha(url)
    path = PDF_DIR / f"{h}.pdf"
    if path.exists() and path.stat().st_size > 1000:
        return path
    if polite_sleep:
        time.sleep(polite_sleep)
    r = requests.get(url, headers={"User-Agent": UA, "Referer": referer}, timeout=90)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"not a PDF ({r.headers.get('content-type')})")
    path.write_bytes(r.content)
    db.x("INSERT OR REPLACE INTO documents(sha,url,path,kind,symbol,title) VALUES(?,?,?,?,?,?)",
         (h, url, str(path), kind, symbol, title))
    return path


def pages_pymupdf(path: Path, max_pages: int = 400) -> list[str]:
    """Return text per page (index 0 = page 1)."""
    import fitz
    out = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            out.append(page.get_text("text") or "")
    return out


def pages_pdfplumber(path: Path, max_pages: int = 40) -> list[str]:
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages):
            if i >= max_pages:
                break
            try:
                out.append(p.extract_text() or "")
            except Exception as e:
                log.warning("pdfplumber page %s failed: %s", i + 1, e)
                out.append("")
    return out
