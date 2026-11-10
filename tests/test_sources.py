"""Phase 1 adapters: hashing, time zones, calendar parsing, screener parsing, scanned-PDF detection, idempotency."""
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import func, select

from finn.sources import bse_filings, calendar, pdfstore, prices, screener
from finn.sources.common import insert_ignore

ROW = {"NEWSID": "c8b0646b", "SCRIP_CD": 532540, "NEWSSUB": "Analyst / Investor Meet - Intimation ",
       "HEADLINE": "Schedule of Analyst Meeting", "MORE": "", "CATEGORYNAME": "Company Update",
       "SUBCATNAME": "Analyst / Investor Meet", "DissemDT": "2026-11-09T16:59:25.653",
       "ATTACHMENTNAME": "c0a1bde0.pdf", "XML_NAME": "ANN_532540_C8B0646B"}


def test_content_hash_is_stable_and_ignores_whitespace():
    assert bse_filings.content_hash(ROW) == bse_filings.content_hash({**ROW, "NEWSSUB": ROW["NEWSSUB"].strip()})
    assert bse_filings.content_hash(ROW) != bse_filings.content_hash({**ROW, "NEWSID": "other"})


def test_filing_times_are_ist_stored_as_utc():
    f = bse_filings.to_filing(ROW, company_id=1)
    assert f["filed_at"] == datetime(2026, 11, 9, 11, 29, 25, tzinfo=timezone.utc)
    assert f["pdf_url"].endswith("/AttachLive/c0a1bde0.pdf")
    assert f["subject"] == "Analyst / Investor Meet - Intimation"
    assert f["body"] == "Schedule of Analyst Meeting"   # MORE empty -> HEADLINE


def test_filing_without_a_date_is_skipped():
    assert bse_filings.to_filing({**ROW, "DissemDT": None, "NEWS_DT": None}, 1) is None


@pytest.mark.parametrize("s,d", [("11 Nov 2026", date(2026, 11, 11)), ("20261111", date(2026, 11, 11)),
                                 ("", None), (None, None), ("garbage", None)])
def test_parse_bse_date(s, d):
    assert calendar.parse_bse_date(s) == d


def test_action_titles_and_kinds():
    assert calendar.action_title("Interim Dividend - Rs. - 7.0000") == "Interim Dividend ₹7/share"
    assert calendar.action_kind("Book Closure for AGM") == "agm"
    assert calendar.action_kind("Bonus issue 1:1") == "ex_date"


@pytest.mark.parametrize("text,d", [
    ("Board Meeting Intimation for considering results on 14th November, 2026", date(2026, 11, 14)),
    ("meeting of the Board scheduled on Friday, 7 November 2026 to consider", date(2026, 11, 7)),
    ("to be held on 05/02/2027 to approve", date(2027, 2, 5)),
    ("Intimation of board meeting", None),
])
def test_meeting_date_from_text(text, d):
    assert calendar.meeting_date_from_text(text) == d


HTML = """<html><h1>Divi's Laboratories Ltd</h1><section id="documents">
<div class="concalls"><ul>
 <li><div>Aug 2026</div><a href="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=abc.pdf">Transcript</a><a href="x">PPT</a></li>
 <li><div>May 2026</div><a href="https://divislabs.com/t.pdf">Transcript</a></li>
 <li><div>Feb 2026</div><a href="https://x/ppt.pdf">PPT</a></li>
</ul></div>
<div class="annual-reports"><ul>
 <li><a href="https://www.bseindia.com/xml-data/corpfiling/AttachHis/ar26.pdf">Financial Year 2026 from bse</a></li>
 <li><a href="https://www.bseindia.com/xml-data/corpfiling/AttachHis/ar25.pdf">Financial Year 2025 from bse</a></li>
 <li><a href="https://www.bseindia.com/xml-data/corpfiling/AttachHis/ar24.pdf">Financial Year 2024 from bse</a></li>
</ul></div></section></html>"""


def test_parse_screener_documents():
    out = screener.parse_documents(HTML, max_transcripts=8, max_ars=2)
    assert out["name"] == "Divi's Laboratories Ltd"
    assert [t["url"] for t in out["transcripts"]] == [
        "https://www.bseindia.com/xml-data/corpfiling/AttachHis/abc.pdf", "https://divislabs.com/t.pdf"]
    assert [t["period"] for t in out["transcripts"]] == ["Q1 FY27", "Q4 FY26"]  # Aug call covers Apr-Jun
    assert [a["period"] for a in out["annual_reports"]] == ["FY26", "FY25"]


def test_screener_page_without_documents_is_unknown_ticker():
    with pytest.raises(screener.NoSuchTicker):
        screener.parse_documents("<html><h1>Not found</h1></html>")


def test_scanned_pdf_detection():
    assert pdfstore.is_scanned(["", "  ", "x" * 50])
    assert not pdfstore.is_scanned(["revenue " * 60, "profit " * 60])
    assert pdfstore.is_scanned([])


def test_price_frame_flattening_skips_nan_rows():
    idx = pd.to_datetime(["2026-11-06", "2026-11-09"])
    cols = pd.MultiIndex.from_tuples([(f, "TCS.NS") for f in ("Adj Close", "Close", "High", "Low", "Open", "Volume")])
    df = pd.DataFrame([[3000, 3010, 3050, 2990, 3000, 1e6], [float("nan")] * 6], index=idx, columns=cols)
    rows = prices.frame_to_rows(df, "TCS.NS", 7)
    assert len(rows) == 1 and rows[0]["date"] == date(2026, 11, 6) and rows[0]["adj_close"] == 3000


def test_filings_insert_is_idempotent(sqldb):
    from finn.database import session
    from finn.models import Company, Filing
    with session() as s:
        s.add(Company(id=1, nse_symbol="TCS", bse_code="532540", name="TCS"))
    row = bse_filings.to_filing(ROW, 1)
    assert insert_ignore(Filing, [row], ["content_hash"]) == 1
    assert insert_ignore(Filing, [row], ["content_hash"]) == 0
    with session() as s:
        assert s.scalar(select(func.count(Filing.id))) == 1


def test_revision_links_to_the_filing_it_supersedes(sqldb):
    from finn.database import session
    from finn.models import Company, Filing
    with session() as s:
        s.add(Company(id=1, nse_symbol="TCS", bse_code="532540", name="TCS"))
    first = bse_filings.to_filing(ROW, 1)
    revised = bse_filings.to_filing({**ROW, "NEWSID": "r2", "NEWSSUB": "Revised: Analyst / Investor Meet - Intimation",
                                     "DissemDT": "2026-11-10T10:00:00"}, 1)
    insert_ignore(Filing, [first, revised], ["content_hash"])
    assert bse_filings.link_revisions(1) == 1
    with session() as s:
        r = s.scalar(select(Filing).where(Filing.exchange_id == "r2"))
        assert s.get(Filing, r.supersedes_id).exchange_id == "c8b0646b"
