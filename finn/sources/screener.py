"""Document links from screener.in's Documents section (probed: docs/probes/screener.json).

Only links are taken from screener.in; every PDF itself is a primary source (BSE or the company's own site).
`.concalls li` rows carry a "Transcript" link; `.annual-reports li a` link annual reports. BSE's
AnnPdfOpen.aspx?Pname=X viewer is rewritten to the direct AttachHis/X PDF.
Manual overrides (data/doc_overrides.csv: symbol,kind,period,url) cover pages screener.in doesn't list.
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from ..fiscal import parse_period, quarter_of
from .common import PoliteHTTP

log = logging.getLogger("finn.sources.screener")
OVERRIDES = Path(__file__).resolve().parents[2] / "data" / "doc_overrides.csv"
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class NoSuchTicker(LookupError):
    pass


def direct_pdf(href: str) -> str:
    if "AnnPdfOpen.aspx" in href:
        name = parse_qs(urlparse(href).query).get("Pname", [""])[0]
        if name:
            return f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{name}"
    return href


def call_period(label: str) -> str | None:
    """A concall row is labelled by the month of the call ("Aug 2026"). Calls discuss the quarter that just
    ended, which is the quarter before the call month's quarter: an Aug 2026 call covers Q1 FY27 (Apr-Jun 2026)."""
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})", label.lower())
    if not m:
        return None
    from datetime import date
    from ..fiscal import prev_quarter
    return prev_quarter(quarter_of(date(int(m.group(2)), _MONTHS[m.group(1)], 1))).label()


def ar_period(title: str) -> str | None:
    """'Annual Report 2026' / 'Financial Year 2026' is the year ending March 2026, i.e. FY26."""
    m = re.search(r"(20\d{2})", title)
    if m:
        return f"FY{int(m.group(1)) % 100:02d}"
    p = parse_period(title)
    return p.label() if p else None


def parse_documents(html: str, max_transcripts: int = 8, max_ars: int = 2) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    docs = soup.select_one("section#documents")
    if not docs:
        raise NoSuchTicker("no Documents section")
    name = (soup.select_one("h1") or soup.new_tag("x")).get_text(strip=True)
    transcripts, seen = [], set()
    for li in docs.select(".concalls li"):
        a = next((a for a in li.select("a") if a.get_text(strip=True).lower().startswith("transcript")), None)
        if not a or not a.get("href"):
            continue
        url = direct_pdf(a["href"])
        if url in seen:
            continue
        seen.add(url)
        label = " ".join(li.get_text(" ", strip=True).split()[:2])
        transcripts.append({"kind": "transcript", "title": f"Earnings call {label}", "period": call_period(label), "url": url})
        if len(transcripts) >= max_transcripts:
            break
    ars = []
    for a in docs.select(".annual-reports li a"):
        href = a.get("href") or ""
        if not href:
            continue
        title = " ".join(a.get_text(" ", strip=True).split())
        ars.append({"kind": "annual_report", "title": title[:120], "period": ar_period(title), "url": direct_pdf(href)})
        if len(ars) >= max_ars:
            break
    return {"name": name, "transcripts": transcripts, "annual_reports": ars}


def overrides(symbol: str) -> list[dict]:
    if not OVERRIDES.exists():
        return []
    return [{"kind": r["kind"], "title": r.get("title") or f"{r['kind']} {r['period']}", "period": r["period"], "url": r["url"]}
            for r in csv.DictReader(OVERRIDES.open(encoding="utf-8")) if r["symbol"].upper() == symbol.upper()]


def list_documents(symbol: str, http: PoliteHTTP | None = None, **kw) -> dict:
    http = http or PoliteHTTP(1.0)
    last = None
    for url in (f"https://www.screener.in/company/{symbol}/consolidated/", f"https://www.screener.in/company/{symbol}/"):
        r = http.get(url, timeout=30)
        last = r.status_code
        if r.ok and 'id="documents"' in r.text:
            out = parse_documents(r.text, **kw)
            out["source_url"] = url
            out["overrides"] = overrides(symbol)
            return out
    raise NoSuchTicker(f"{symbol}: screener.in returned {last}")
