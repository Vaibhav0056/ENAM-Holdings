"""NSE adapters: the fallbacks when BSE is unavailable (probed 2026-11-15 with FINN's own User-Agent, all 200).

- corporate announcements API  www.nseindia.com/api/corporate-announcements?symbol=&from_date=&to_date=
- announcements RSS            nsearchives.nseindia.com/content/RSS/Online_announcements.xml (latest only, all cos)
- event calendar API           www.nseindia.com/api/event-calendar (board meetings, results dates)
- bhavcopy archive             nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip

Every parser is pure (text in, rows out) and tested against recorded responses in tests/fixtures/sources/.
Rows use the same schemas as the BSE / yfinance adapters, so the rest of FINN cannot tell where a row came from
except by its `exchange` / `source` field.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

from ..fiscal import to_utc

API = "https://www.nseindia.com/api/corporate-announcements"
RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
EVENTS = "https://www.nseindia.com/api/event-calendar"
BHAV = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
REFERER = "https://www.nseindia.com/"


def _when(s: str | None) -> datetime | None:
    """'09-Nov-2026 17:12:15' (IST wall clock) -> aware UTC."""
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return to_utc(datetime.strptime(s.strip(), fmt))
        except ValueError:
            continue
    return None


def _hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p or "").strip() for p in parts).encode("utf-8")).hexdigest()


def filing_row(company_id: int, symbol: str, exchange_id: str, when: datetime, category: str | None,
               text: str | None, pdf: str | None, raw: dict) -> dict:
    return {"company_id": company_id, "exchange": "NSE", "exchange_id": str(exchange_id or ""), "filed_at": when,
            "category_raw": (category or "").strip()[:120] or None, "subcategory_raw": None,
            "subject": (category or text or "Announcement").strip(), "body": (text or "").strip() or None,
            "pdf_url": pdf or None, "xbrl_url": None, "content_hash": _hash("NSE", symbol, exchange_id, pdf),
            "status": "new", "raw": raw}


# ---------- announcements API ----------

def parse_api(rows: list[dict], company_id: int) -> list[dict]:
    out = []
    for r in rows or []:
        when = _when(r.get("an_dt") or r.get("exchdisstime"))
        if not when:
            continue
        out.append(filing_row(company_id, r.get("symbol"), r.get("seq_id"), when, r.get("desc"),
                              r.get("attchmntText"), r.get("attchmntFile"),
                              {k: r.get(k) for k in ("seq_id", "symbol", "desc", "an_dt", "attchmntFile", "hasXbrl", "sm_isin")}))
    return out


def api_announcements(http, companies, days: int) -> tuple[list[dict], list]:
    to, fr = date.today(), date.today() - timedelta(days=days)
    rows, failed = [], []
    for c in companies:
        try:
            r = http.get(f"{API}?index=equities&symbol={c.nse_symbol}&from_date={fr:%d-%m-%Y}&to_date={to:%d-%m-%Y}",
                         referer=REFERER, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"NSE API {r.status_code}")
            rows += parse_api(r.json(), c.id)
        except Exception:  # noqa: BLE001 - the chain hands this company to the next source
            failed.append(c)
    return rows, failed


# ---------- announcements RSS (latest ~ day, every company) ----------

def parse_rss(xml_text: str, by_symbol: dict[str, int]) -> list[dict]:
    """RSS items carry the company name as title and the PDF link; the NSE symbol is the PDF file's prefix
    (".../corporate/TCS_CORPCS_09112026171125_....pdf")."""
    out = []
    root = ElementTree.fromstring(xml_text)
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        sym = link.rsplit("/", 1)[-1].split("_", 1)[0].upper() if link else ""
        cid = by_symbol.get(sym)
        when = _when(it.findtext("pubDate"))
        if not cid or not when:
            continue
        desc = (it.findtext("description") or "").strip()
        subject = desc.split("|SUBJECT:", 1)[1].strip() if "|SUBJECT:" in desc else None
        text = desc.split("|SUBJECT:", 1)[0].strip()
        out.append(filing_row(cid, sym, link.rsplit("/", 1)[-1], when, subject, text, link,
                              {"title": it.findtext("title"), "link": link, "pubDate": it.findtext("pubDate")}))
    return out


def rss_announcements(http, companies, days: int) -> tuple[list[dict], list]:
    r = http.get(RSS, referer=REFERER, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"NSE RSS {r.status_code}")
    by_symbol = {c.nse_symbol.upper(): c.id for c in companies}
    return parse_rss(r.text, by_symbol), []          # the feed covers everyone at once; nothing is left over


# ---------- event calendar ----------

def parse_events(rows: list[dict], by_symbol: dict[str, int]) -> list[dict]:
    out = []
    for r in rows or []:
        cid = by_symbol.get((r.get("symbol") or "").upper())
        try:
            d = datetime.strptime((r.get("date") or "").strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        if not cid:
            continue
        purpose = (r.get("purpose") or "Board meeting").strip()
        results = bool(re.search(r"result", purpose + " " + (r.get("bm_desc") or ""), re.I))
        out.append({"company_id": cid, "kind": "results" if results else "board_meeting", "event_date": d,
                    "title": "Board meeting: results" if results else f"Board meeting: {purpose}"[:300],
                    "source": "nse_event_calendar"})
    return out


def events(http, companies) -> list[dict]:
    r = http.get(EVENTS, referer=REFERER, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"NSE event calendar {r.status_code}")
    return parse_events(r.json(), {c.nse_symbol.upper(): c.id for c in companies})


# ---------- bhavcopy prices ----------

def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def parse_bhavcopy(csv_text: str, by_symbol: dict[str, tuple[int, str]]) -> list[dict]:
    """by_symbol: NSE symbol -> (company_id, yahoo symbol). EQ series only. adj_close = close: bhavcopy is not
    adjusted for splits/bonuses, which matters only across a corporate action (yfinance overwrites when it returns)."""
    out = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        hit = by_symbol.get((r.get("TckrSymb") or "").upper())
        if not hit or (r.get("SctySrs") or "").strip() != "EQ":
            continue
        close = _f(r.get("ClsPric"))
        out.append({"company_id": hit[0], "symbol": hit[1], "date": datetime.strptime(r["TradDt"], "%Y-%m-%d").date(),
                    "open": _f(r.get("OpnPric")), "high": _f(r.get("HghPric")), "low": _f(r.get("LwPric")),
                    "close": close, "adj_close": close, "volume": _f(r.get("TtlTradgVol"))})
    return out


def bhavcopy_prices(http, companies, days: int) -> tuple[list[dict], list]:
    by_symbol = {c.nse_symbol.upper(): (c.id, c.yahoo_symbol or f"{c.nse_symbol}.NS") for c in companies}
    rows, got_any = [], False
    for back in range(days + 1):
        d = date.today() - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        r = http.get(BHAV.format(d=d), referer=REFERER, timeout=30, attempts=1)
        if r.status_code == 404:          # holiday or not yet published
            continue
        if r.status_code != 200:
            raise RuntimeError(f"bhavcopy {d} {r.status_code}")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            rows += parse_bhavcopy(z.read(z.namelist()[0]).decode("utf-8", "replace"), by_symbol)
        got_any = True
    if not got_any:
        raise RuntimeError("no bhavcopy file in the window")
    have = {r["company_id"] for r in rows}
    return rows, [c for c in companies if c.id not in have]
