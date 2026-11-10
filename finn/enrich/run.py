"""Enrichment pass for cards that need more than a one-liner (Phase 3).

- results (the primary filing of each results event): the results table, verified.
- mgmt_change: people (batched model call), cited profile, governance flags for senior exits.
- governance / litigation / pledge filings: rule-based red flags from the filing's wording.
- order_win: order value from the text ÷ trailing revenue (BSE snapshot) for the magnitude factor.

Every item fails soft: a scanned or unreachable PDF becomes payload {ok: false, reason}, shown as such on the card.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select

from ..database import session
from ..models import Company, Enrichment, Filing
from . import docs, governance, mgmt, results

log = logging.getLogger("finn.enrich.run")
METRIC_NAME = {"revenue": "Revenue", "ebitda": "EBITDA", "pbt": "PBT", "pat": "PAT"}
_INR_RE = re.compile(r"(?:₹|\brs\.?|\binr)\s?([\d,]+(?:\.\d+)?)\s*(crore|cr\b|lakh)|([\d,]+(?:\.\d+)?)\s*(crore|cr\b)", re.I)


def order_value_cr(text: str) -> float | None:
    vals = []
    for m in _INR_RE.finditer(text or ""):
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        vals.append(v / 100 if unit.lower().startswith("lakh") else v)
    return max(vals) if vals else None


def results_headline(p: dict) -> str | None:
    if not p.get("ok"):
        return None
    parts = []
    for row in p["table"]:
        if row["metric"] in ("revenue", "pat") and row.get("found"):
            yoy = f" ({row['yoy']:+.1f}% YoY)" if row.get("yoy") is not None else ""
            parts.append(f"{METRIC_NAME[row['metric']]} ₹{row['current']:,.0f} cr{yoy}")
    return (f"{p.get('period') or 'Quarterly'} results: " + ", ".join(parts)) if parts else None


def people_headline(people: list[dict]) -> str | None:
    named = [p for p in people if p.get("role")]
    if not named:
        return None
    p = named[0]
    verb = {"appointed": "appointed", "resigned": "resigns", "retired": "retires", "ceased": "ceases",
            "redesignated": "re-designated"}.get(p["direction"], "change")
    more = f" (+{len(named) - 1} more)" if len(named) > 1 else ""
    return f"{p['name']}, {p['role']}: {verb}{more}"[:180]


def pending(limit: int | None = None) -> list[tuple[Filing, Enrichment]]:
    with session() as s:
        q = (select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
             .where(Enrichment.payload.is_(None))
             .where(Enrichment.type.in_(["results", "mgmt_change", "governance", "litigation_regulatory",
                                         "insider_pledge", "order_win"]))
             .order_by(Filing.filed_at.desc()))
        rows = [(f, e) for f, e in s.execute(q).all()
                if e.type != "results" or (e.score_breakdown or {}).get("novelty") == 1.0]
    return rows[:limit] if limit else rows


def run(use_llm: bool = True, limit: int | None = None) -> dict:
    todo = pending(limit)
    stats = {"results": 0, "results_ok": 0, "mgmt": 0, "flags": 0, "orders": 0, "failed": 0, "llm_free_results": 0}
    with session() as s:
        companies = {c.id: c for c in s.scalars(select(Company))}
    mgmt_batch: list[dict] = []
    for f, e in todo:
        try:
            if e.type == "results":
                stats["results"] += 1
                payload = _results(f, companies.get(f.company_id), use_llm)
                stats["results_ok"] += bool(payload.get("ok"))
                stats["llm_free_results"] += "BSE" in (payload.get("source") or "")
                _save(f.id, payload, results_headline(payload), payload.get("confidence"))
            elif e.type == "mgmt_change":
                pages = _filing_pages(f)
                mgmt_batch.append({"id": f.id, "subject": f.subject, "body": f.body, "pages": pages or [],
                                   "company": companies[f.company_id].name})
            elif e.type == "order_win":
                v = order_value_cr(f"{f.subject} {f.body}")
                ttm = _ttm(companies.get(f.company_id))
                _save(f.id, {"kind": "order", "ok": v is not None, "value_cr": v, "ttm_revenue_cr": ttm,
                             "share_of_revenue": round(v / ttm, 4) if v and ttm else None}, None, None)
                stats["orders"] += 1
            else:
                flags = governance.rule_flags(e.type, f.subject, f.body)
                _save(f.id, {"kind": "governance", "ok": True, "flags": flags}, None, None)
                stats["flags"] += len(flags)
        except Exception as ex:  # noqa: BLE001 - one bad filing never stops the pass
            log.exception("enrichment failed for filing %s", f.id)
            _save(f.id, {"ok": False, "reason": f"couldn't process: {ex}"[:300]}, None, "low")
            stats["failed"] += 1
    if mgmt_batch:
        found = mgmt.extract_batch(mgmt_batch, use_llm=use_llm)
        by_id = {m["id"]: m for m in mgmt_batch}
        for fid, people in found.items():
            f_row = by_id[fid]
            for p in people:
                if p.get("direction") == "appointed" and p.get("senior"):
                    p["web_bio"] = mgmt.web_bio(p["name"], f_row["company"])
            flags = mgmt.governance_flags(people) + governance.rule_flags("mgmt_change", f_row["subject"], f_row["body"])
            payload = {"kind": "mgmt_change", "ok": bool(people), "people": people, "governance_flags": flags,
                       "pdf_pages": len(f_row["pages"]), "source": people[0].get("source") if people else None}
            if not f_row["pages"]:
                payload["reason"] = "filing PDF unavailable or scanned"
            _save(fid, payload, people_headline(people), None)
            stats["mgmt"] += 1
            stats["flags"] += len(flags)
    return stats


def _filing_pages(f: Filing) -> list[str] | None:
    try:
        doc_id, status = docs.filing_document(f.id)
    except docs.NoPDF:
        return None
    return docs.pages(doc_id) if status == "parsed" else None


def _ttm(c: Company | None) -> float | None:
    from ..sources.bse_snapshot import ttm_revenue
    return ttm_revenue(c.bse_snapshot) if c else None


def _results(f: Filing, c: Company | None, use_llm: bool) -> dict:
    try:
        doc_id, status = docs.filing_document(f.id)
    except docs.NoPDF:
        return {"ok": False, "reason": "no PDF attached to the filing"}
    if status == "unparseable":
        return {"ok": False, "reason": "scanned PDF with no text layer: figures not extracted"}
    if status not in ("parsed", "duplicate"):
        return {"ok": False, "reason": f"PDF could not be fetched ({status})"}
    path = docs.pdf_path(doc_id)
    pages = docs.plumber_pages(path) if path else docs.pages(doc_id)
    from ..fiscal import to_ist
    out = results.extract(pages, f.subject, c.bse_snapshot if c else None, use_llm=use_llm, budget_key=f"filing:{f.id}",
                          filed_on=to_ist(f.filed_at).date())
    out["document_id"] = doc_id
    return out


def _save(filing_id: int, payload: dict, headline: str | None, confidence: str | None):
    with session() as s:
        e = s.get(Enrichment, filing_id)
        e.payload = payload
        if headline:
            e.headline = headline
        if confidence:
            e.confidence = confidence
