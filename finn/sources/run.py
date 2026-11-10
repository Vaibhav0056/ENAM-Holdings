"""Phase 1 ingest: prices, BSE filings, catalysts calendar, document links, and the latest transcript per company.

    python -m finn.sources.run              # everything, 90 days
    python -m finn.sources.run prices bse   # only some sources

Idempotent: every table has a natural unique key and inserts are ON CONFLICT DO NOTHING (prices DO UPDATE), so a
second run reports 0 added. Annual reports and older transcripts are listed here but fetched on demand by research
(Phase 4): the latest transcript per company is enough to prove the fetcher and keeps the cold ingest polite.
"""
from __future__ import annotations

import json
import logging
import sys
import time

from sqlalchemy import select

from ..database import session
from ..models import Company, Document
from . import bse_filings, bse_snapshot, calendar, prices, screener
from .common import PoliteHTTP, insert_ignore, log_run
from .pdfstore import fetch_document

log = logging.getLogger("finn.sources.run")


def companies() -> list[Company]:
    with session() as s:
        return list(s.scalars(select(Company).order_by(Company.is_portfolio.desc(), Company.nse_symbol)))


def list_docs(cos, http: PoliteHTTP) -> dict[str, dict]:
    out = {}
    for c in cos:
        try:
            found = screener.list_documents(c.nse_symbol, http)
            rows = [{"company_id": c.id, "kind": d["kind"], "period": d["period"], "title": d["title"], "url": d["url"],
                     "status": "listed"} for d in found["transcripts"] + found["annual_reports"] + found["overrides"]]
            added = insert_ignore(Document, rows, ["company_id", "url"])
            out[c.nse_symbol] = {"transcripts": len(found["transcripts"]), "annual_reports": len(found["annual_reports"]),
                                 "added": added}
            log_run("screener_docs", c.id, added, len(rows))
        except Exception as e:  # noqa: BLE001
            log.warning("documents failed %s: %s", c.nse_symbol, e)
            out[c.nse_symbol] = {"error": str(e)[:200]}
            log_run("screener_docs", c.id, 0, 0, ok=False, error=str(e))
    return out


def fetch_latest_transcripts(cos, http: PoliteHTTP) -> dict[str, dict]:
    """Newest transcript that can actually be fetched. Some companies host the newest PDF on their own site, which
    can refuse automated requests (APOLLOHOSP: 403); that one stays `failed` with its reason and the next listed
    transcript is tried instead. Blocks are respected, never worked around."""
    out = {}
    with session() as s:
        listed = {c.nse_symbol: list(s.scalars(select(Document.id).where(Document.company_id == c.id,
                                                                         Document.kind == "transcript")
                                                .order_by(Document.id)))  # screener lists newest first
                  for c in cos}
    for sym, ids in listed.items():
        if not ids:
            out[sym] = {"status": "none listed"}
            continue
        skipped = []
        for doc_id in ids[:3]:
            res = fetch_document(doc_id, http)
            if res["status"] in ("parsed", "unparseable"):
                out[sym] = {**res, "skipped": skipped}
                break
            skipped.append({"id": doc_id, "status": res["status"], "error": res.get("error", "")[:80]})
        else:
            out[sym] = {"status": "failed", "error": skipped[-1]["error"] if skipped else "", "skipped": skipped}
    return out


def main(which: list[str]) -> dict:
    which = which or ["prices", "bse", "snapshot", "calendar", "docs", "pdfs"]
    cos = companies()
    http = PoliteHTTP(1.0)
    report, t0 = {}, time.time()
    if "prices" in which:
        report["prices"] = prices.ingest(cos, 90)
    if "bse" in which:
        report["bse"] = bse_filings.ingest(cos, 90)
    if "snapshot" in which:
        report["snapshot"] = bse_snapshot.ingest(cos)
    if "calendar" in which:
        report["calendar"] = calendar.ingest(cos)
    if "docs" in which:
        report["docs"] = list_docs(cos, http)
    if "pdfs" in which:
        report["pdfs"] = fetch_latest_transcripts(cos, http)
    report["seconds"] = round(time.time() - t0, 1)
    return report


def print_table(report: dict):
    syms = sorted({s for k in ("prices", "bse", "docs", "pdfs") for s in report.get(k, {})})
    print(f"{'symbol':12} {'price+':>6} {'filings':>7} {'filing+':>7} {'transcr':>7} {'ARs':>4} {'latest transcript':>18}")
    tot = {"p": 0, "f": 0, "fa": 0}
    for s in syms:
        p, b, d, pdf = (report.get(k, {}).get(s, {}) for k in ("prices", "bse", "docs", "pdfs"))
        tot["p"] += p.get("added", 0)
        tot["f"] += b.get("seen", 0)
        tot["fa"] += b.get("added", 0)
        latest = pdf.get("status", "-") + (f" {pdf.get('pages')}p" if pdf.get("pages") else "")
        err = next((x.get("error") for x in (p, b, d, pdf) if x.get("error")), "")
        print(f"{s:12} {p.get('added', '-'):>6} {b.get('seen', '-'):>7} {b.get('added', '-'):>7} "
              f"{d.get('transcripts', '-'):>7} {d.get('annual_reports', '-'):>4} {latest:>18} {err[:60]}")
    print(f"TOTAL price rows added {tot['p']}, filings seen {tot['f']}, filings added {tot['fa']}")
    if "calendar" in report:
        print("calendar:", json.dumps(report["calendar"]))
    print("seconds:", report["seconds"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    rep = main(sys.argv[1:])
    print_table(rep)
