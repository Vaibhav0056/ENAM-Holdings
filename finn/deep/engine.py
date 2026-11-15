"""Deep dives (spec R5): one engine, eight recipes, each retrieve → extract → compute → explain → verify.

    python -m finn.deep.engine POLYCAB hedge_meter|language_diff|dodge_detector|cash_quality|balance_sheet|
                                       worry_map|fine_print|peer_compare [--force]

Every figure is computed in finn.deep.maths from inputs whose quote was verified on its page (figures must be
printed in the quote). Results are cached as reports(kind='deep:<recipe>') for 24 h.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlalchemy import select, text

from .. import fiscal
from ..database import engine as db_engine
from ..database import session
from ..grounding import value_in_quote, verify
from ..llm import router
from ..models import Chunk, Company, Document, LLMCall, Page, Report
from . import maths

log = logging.getLogger("finn.deep")
TTL = timedelta(days=float(__import__('os').getenv('REPORT_TTL_DAYS', '7')))   # same inputs as the report: quarterly
RECIPES = ["cash_quality", "language_diff", "hedge_meter", "dodge_detector", "balance_sheet", "worry_map",
           "fine_print", "peer_compare"]
TITLES = {"cash_quality": "Cash quality", "language_diff": "Language diff", "hedge_meter": "Hedge-meter",
          "dodge_detector": "Dodge detector", "balance_sheet": "Balance sheet", "worry_map": "Analyst worry map",
          "fine_print": "Fine-print changes", "peer_compare": "Peer compare"}


class NeedsResearch(RuntimeError):
    """A deep dive that this company's documents cannot support yet (a designed state, not an error). `research`
    lists symbols whose research report would make it possible."""
    def __init__(self, message: str, research: list[str] | None = None):
        super().__init__(message)
        self.research = research or []


# ---------- documents ----------

def company(symbol: str) -> Company:
    with session() as s:
        c = s.scalar(select(Company).where(Company.nse_symbol == symbol.upper()))
        if not c:
            raise NeedsResearch(f"{symbol} has no documents yet: open its research first")
        s.expunge(c)
        return c


def docs(cid: int, kind: str, n: int) -> list[dict]:
    with session() as s:
        rows = s.scalars(select(Document).where(Document.company_id == cid, Document.kind == kind, Document.status == "parsed")
                         .order_by(Document.id)).all()[:n]
        return [{"id": d.id, "period": d.period, "title": d.title, "url": d.url} for d in rows]


def pages_of(doc_id: int) -> list[str]:
    with session() as s:
        return [t for _, t in s.execute(select(Page.page_no, Page.text).where(Page.document_id == doc_id).order_by(Page.page_no))]


def top_pages(doc_id: int, query: str, k: int = 4, prefer: str | None = "consolidated") -> list[int]:
    """Keyword (full-text) page retrieval inside one document; pages mentioning `prefer` rank first."""
    if db_engine().dialect.name != "postgresql":
        return []
    with db_engine().connect() as c:
        rows = c.execute(text("""
            SELECT page_no, ts_rank_cd(tsv, websearch_to_tsquery('english', :q)) * (CASE WHEN :p <> '' AND text ILIKE '%' || :p || '%' THEN 2 ELSE 1 END) AS r
            FROM pages WHERE document_id = :d AND tsv @@ websearch_to_tsquery('english', :q) ORDER BY r DESC LIMIT :k"""),
            {"q": query, "d": doc_id, "k": k, "p": prefer or ""}).all()
    return [r[0] for r in rows]


# ---------- schemas ----------

FIN_ITEMS = Literal["revenue", "pat", "pbt", "depreciation", "finance_costs", "other_income", "ocf", "capex",
                    "receivables", "inventories", "payables", "borrowings_long", "borrowings_short", "cash",
                    "current_investments", "equity", "contingent_liabilities"]


class FinItem(BaseModel):
    item: FIN_ITEMS
    year: Literal["current", "previous"]
    value: float
    unit: str
    basis: Literal["consolidated", "standalone"]
    quote: str
    page: int


class Financials(BaseModel):
    fiscal_year: str
    items: list[FinItem]


class QALabel(BaseModel):
    id: int
    label: Literal["answered", "partial", "deflected"]
    evidence: str


class QALabels(BaseModel):
    items: list[QALabel]


class TopicLabel(BaseModel):
    id: int
    topic: Literal["demand", "pricing and margins", "raw materials and costs", "capex and capacity", "working capital and cash",
                   "competition", "exports and geography", "new businesses", "guidance", "regulation and policy",
                   "management and governance", "capital allocation", "other"]


class TopicLabels(BaseModel):
    items: list[TopicLabel]


class FineItem(BaseModel):
    kind: Literal["key_audit_matter", "emphasis_of_matter", "qualification", "related_party", "contingent_liability"]
    title: str
    amount: Optional[float] = None
    unit: Optional[str] = None
    quote: str
    page: int


class FinePrint(BaseModel):
    items: list[FineItem]


# ---------- recipes ----------

# websearch_to_tsquery ANDs bare words, so each query ORs quoted phrases: a statement page rarely has every term.
FIN_QUERIES = ['"net cash generated from operating activities" or "cash flows from operating activities" or "purchase of property"',
               '"balance sheet" or "trade receivables" or "total equity" or "borrowings"',
               '"statement of profit and loss" or "revenue from operations" or "finance costs"',
               '"contingent liabilities"']
FINE_QUERIES = ['"key audit matters" or "key audit matter"', '"emphasis of matter" or "qualified opinion" or "material uncertainty"',
                '"related party transactions" or "related party disclosures"', '"contingent liabilities"']


def _financials(c: Company, budget: str) -> dict:
    ars = docs(c.id, "annual_report", 1)
    if not ars:
        raise NeedsResearch("no annual report is stored yet: open the company's research first")
    ar = ars[0]
    version = router.load_prompt("financials_extract")[0]
    with session() as s:
        from ..models import Extraction
        hit = s.scalar(select(Extraction).where(Extraction.document_id == ar["id"], Extraction.schema == "financials",
                                                Extraction.prompt_version == version))
        if hit:
            return hit.data
    pages = pages_of(ar["id"])
    # Two focused calls (flows: cash flow + P&L; stocks: balance sheet + notes). One call over every page returned
    # ~20 figures and dropped PAT and the working-capital lines, the same crowding the promise extractor showed.
    groups = [[FIN_QUERIES[0], FIN_QUERIES[2]], [FIN_QUERIES[1], FIN_QUERIES[3]]]
    raw, fy = [], None
    for qs in groups:
        sel = sorted({p for q in qs for p in top_pages(ar["id"], q, 4)})[:10]
        if not sel:
            continue
        doc = "\n\n".join(f"[PAGE {p}]\n{pages[p - 1][:7000]}" for p in sel)
        r = router.call("deep_extract", "financials_extract", {"report": ar["title"] or ar["period"], "document": doc},
                        Financials, budget_key=budget)
        raw += [i.model_dump() for i in r.data.items]
        fy = fy or r.data.fiscal_year
    if not raw:
        raise NeedsResearch("couldn't find the financial statements in the annual report text")
    r = type("R", (), {"data": type("D", (), {"fiscal_year": fy})()})()
    sel = sorted({p for q in FIN_QUERIES for p in top_pages(ar["id"], q, 4)})[:20]
    kept = [i for i in verify(raw, pages) if value_in_quote(i["value"], i["quote"])]
    basis = "consolidated" if any(i["basis"] == "consolidated" for i in kept) else "standalone"
    years: dict[str, dict] = {"current": {}, "previous": {}}
    evidence: dict[str, dict] = {"current": {}, "previous": {}}
    for i in kept:
        if i["basis"] != basis:
            continue
        cr = fiscal.to_crore(i["value"], i["unit"])
        if cr is None or i["item"] in years[i["year"]]:
            continue
        years[i["year"]][i["item"]] = cr
        evidence[i["year"]][i["item"]] = {"quote": i["quote"], "page": i["page"], "as_printed": f"{i['value']} {i['unit']}"}
    out = {"document_id": ar["id"], "report": ar["title"], "fiscal_year": r.data.fiscal_year, "basis": basis,
           "years": years, "evidence": evidence, "extracted": len(raw), "kept": len(kept), "pages_read": sel}
    from ..sources.common import insert_ignore
    from ..models import Extraction
    insert_ignore(Extraction, [{"document_id": ar["id"], "schema": "financials", "prompt_version": version, "data": out,
                                "verified_ratio": len(kept) / len(raw) if raw else None}], ["document_id", "schema", "prompt_version"])
    return out


def _with_ebitda(y: dict) -> dict:
    y = dict(y)
    if all(k in y for k in ("pbt", "finance_costs", "depreciation")):
        y["ebitda"] = round(y["pbt"] + y["finance_costs"] + y["depreciation"] - (y.get("other_income") or 0), 2)
    return y


def cash_quality(c: Company, budget: str) -> dict:
    f = _financials(c, budget)
    cur, prev = _with_ebitda(f["years"]["current"]), _with_ebitda(f["years"]["previous"])
    m_cur, m_prev = maths.cash_quality(cur), maths.cash_quality(prev)
    return {"fiscal_year": f["fiscal_year"], "basis": f["basis"], "unit": "₹ crore", "current": m_cur, "previous": m_prev,
            "inputs": {"current": cur, "previous": prev}, "evidence": f["evidence"], "document_id": f["document_id"],
            "report": f["report"], "note": "Days use revenue when cost of goods sold is not extracted; the basis is stated.",
            "verified": f"{f['kept']} of {f['extracted']} extracted figures verified on their page"}


def balance_sheet(c: Company, budget: str) -> dict:
    f = _financials(c, budget)
    cur, prev = _with_ebitda(f["years"]["current"]), _with_ebitda(f["years"]["previous"])
    return {"fiscal_year": f["fiscal_year"], "basis": f["basis"], "unit": "₹ crore",
            "current": maths.balance_sheet(cur), "previous": maths.balance_sheet(prev),
            "inputs": {"current": cur, "previous": prev}, "evidence": f["evidence"], "document_id": f["document_id"],
            "report": f["report"], "verified": f"{f['kept']} of {f['extracted']} extracted figures verified on their page",
            "note": "Debt maturities are not extracted yet; contingent liabilities are as disclosed in the notes."}


def _mgmt_turns(doc_id: int) -> list[dict]:
    from ..research.transcripts import split_turns
    return [t for t in split_turns(pages_of(doc_id)) if t["role"] == "management"]


def hedge_meter(c: Company, budget: str) -> dict:
    calls = docs(c.id, "transcript", 8)
    if not calls:
        raise NeedsResearch("no earnings-call transcripts stored")
    rows = []
    for d in calls:
        r = maths.hedge_rate([t["text"] for t in _mgmt_turns(d["id"])])
        rows.append({"call": d["period"], "document_id": d["id"], **r})
    rows.sort(key=lambda x: fiscal.parse_period(x["call"]) or fiscal.Period(0))
    rates = [r["per_1000_words"] for r in rows if r["per_1000_words"] is not None]
    trend = None
    if len(rates) >= 3:
        base = sum(rates[:-1]) / len(rates[:-1])
        trend = round((rates[-1] - base) / base * 100, 1) if base else None
    return {"calls": rows, "latest_vs_earlier_pct": trend, "lexicon": maths.HEDGES,
            "method": "cautious phrases per 1,000 words of management remarks; pure Python, no model"}


def language_diff(c: Company, budget: str) -> dict:
    from ..retrieval import ensure_embedded
    calls = docs(c.id, "transcript", 2)
    if len(calls) < 2:
        raise NeedsResearch("needs two earnings-call transcripts")
    newer, older = calls[0], calls[1]
    ensure_embedded([newer["id"], older["id"]], budget)

    def stmts(d):
        with session() as s:
            rows = s.execute(select(Chunk.text, Chunk.embedding, Chunk.page_from, Chunk.speaker)
                             .where(Chunk.document_id == d["id"], Chunk.role == "management", Chunk.embedding.is_not(None))).all()
        return [{"text": t, "embedding": e, "page": p, "speaker": sp, "call": d["period"], "document_id": d["id"]}
                for t, e, p, sp in rows if len(t) > 80]
    diff = maths.language_diff(stmts(older), stmts(newer))
    return {"older": older["period"], "newer": newer["period"], **diff,
            "method": "management statements aligned by embedding similarity; below 0.8 on the other call = dropped or new"}


def dodge_detector(c: Company, budget: str) -> dict:
    from ..research.transcripts import qa_pairs, split_turns
    calls = docs(c.id, "transcript", 2)
    if not calls:
        raise NeedsResearch("no earnings-call transcripts stored")
    per_call, flagged = [], []
    for d in calls:
        pairs = qa_pairs(split_turns(pages_of(d["id"])))[:36]
        labels: dict[int, QALabel] = {}
        for i in range(0, len(pairs), 12):
            chunk = pairs[i:i + 12]
            doc = "\n\n".join(f"[ID {i + j}]\nQ ({p['analyst']}): {p['question'][:700]}\nA ({p['answered_by']}): {p['answer'][:1200]}"
                              for j, p in enumerate(chunk))
            r = router.call("dodge_label", "dodge_label", {"document": doc}, QALabels, budget_key=budget)
            for x in r.data.items:
                if 0 <= x.id - i < len(chunk):
                    labels[x.id] = x
        counts = {"answered": 0, "partial": 0, "deflected": 0}
        for idx, p in enumerate(pairs):
            lab = labels.get(idx)
            if not lab:
                continue
            # the evidence must be words management actually said in that answer
            if fuzz.partial_ratio(lab.evidence.lower(), p["answer"].lower()) < 90:
                continue
            counts[lab.label] += 1
            if lab.label != "answered":
                flagged.append({"call": d["period"], "label": lab.label, "analyst": p["analyst"], "question": p["question"][:400],
                                "answered_by": p["answered_by"], "evidence": lab.evidence, "page": p["answer_page"],
                                "document_id": d["id"]})
        n = sum(counts.values())
        per_call.append({"call": d["period"], "pairs": len(pairs), "judged": n, **counts,
                         "deflection_rate_pct": round(100 * counts["deflected"] / n, 1) if n else None})
    flagged.sort(key=lambda x: x["label"] != "deflected")
    return {"calls": per_call, "flagged": flagged[:12],
            "method": "each analyst question paired with management's answer; the model labels it and must quote the answer"}


def worry_map(c: Company, budget: str) -> dict:
    from ..research.transcripts import qa_pairs, split_turns
    calls = docs(c.id, "transcript", 6)
    if not calls:
        raise NeedsResearch("no earnings-call transcripts stored")
    labels = []
    for d in calls:
        qs = [p["question"] for p in qa_pairs(split_turns(pages_of(d["id"])))][:40]
        if not qs:
            continue
        doc = "\n".join(f"[ID {i}] {q[:400]}" for i, q in enumerate(qs))
        r = router.call("deep_extract", "worry_topic", {"document": doc}, TopicLabels, budget_key=budget)
        labels += [{"quarter": d["period"], "topic": x.topic} for x in r.data.items if 0 <= x.id < len(qs)]
    return {**maths.worry_grid(labels), "questions": len(labels),
            "method": "each analyst question gets one topic from a fixed list; counts are per call"}


def fine_print(c: Company, budget: str) -> dict:
    ars = docs(c.id, "annual_report", 2)
    if len(ars) < 2:
        raise NeedsResearch("needs two annual reports")
    years = []
    for ar in ars:
        pages = pages_of(ar["id"])
        sel = sorted({p for q in FINE_QUERIES for p in top_pages(ar["id"], q, 3, None)})[:10]
        if not sel:
            years.append({"report": ar["title"], "period": ar["period"], "items": []})
            continue
        doc = "\n\n".join(f"[PAGE {p}]\n{pages[p - 1][:6000]}" for p in sel)
        r = router.call("deep_extract", "fine_print", {"report": ar["title"] or ar["period"], "document": doc}, FinePrint,
                        budget_key=budget)
        items = [i for i in verify([x.model_dump() for x in r.data.items], pages)
                 if i.get("amount") is None or value_in_quote(i["amount"], i["quote"])]
        for i in items:
            i["amount_cr"] = fiscal.to_crore(i["amount"], i.get("unit")) if i.get("amount") is not None else None
            i["document_id"] = ar["id"]
        years.append({"report": ar["title"], "period": ar["period"], "items": items})
    new, old = years[0], years[1]

    def titles(y, kind):
        return [i for i in y["items"] if i["kind"] == kind]
    new_kams = [i for i in titles(new, "key_audit_matter")
                if not any(fuzz.token_set_ratio(i["title"], o["title"]) >= 75 for o in titles(old, "key_audit_matter"))]
    cl_new = sum(i["amount_cr"] or 0 for i in titles(new, "contingent_liability"))
    cl_old = sum(i["amount_cr"] or 0 for i in titles(old, "contingent_liability"))
    return {"years": years, "new_key_audit_matters": new_kams,
            "qualifications_or_emphasis": [i for i in new["items"] if i["kind"] in ("qualification", "emphasis_of_matter")],
            "contingent_liabilities_cr": {"latest": round(cl_new, 2) or None, "previous": round(cl_old, 2) or None,
                                          "change_pct": round((cl_new - cl_old) / cl_old * 100, 1) if cl_old else None},
            "method": "auditor remarks, related-party items and contingent liabilities from two annual reports; changes in Python"}


def peer_compare(c: Company, budget: str) -> dict:
    from ..research.engine import cached_report
    with session() as s:
        peer = None
        if c.peer_of:
            peer = s.get(Company, c.peer_of)
        else:
            peer = s.scalar(select(Company).where(Company.peer_of == c.id))
        peer_sym = peer.nse_symbol if peer else None
    if not peer_sym:
        raise NeedsResearch("no peer is configured for this company")
    a, b = cached_report(c.nse_symbol), cached_report(peer_sym)
    missing = [s for s, r in ((c.nse_symbol, a), (peer_sym, b)) if not r]
    if missing:
        raise NeedsResearch(f"Peer compare reads both companies' research reports; {' and '.join(missing)} "
                            f"{'has' if len(missing) == 1 else 'have'} none yet", research=list(missing))
    return {**maths.compare_ledgers(a, b), "peer": peer_sym}


RUNNERS = {"cash_quality": cash_quality, "language_diff": language_diff, "hedge_meter": hedge_meter,
           "dodge_detector": dodge_detector, "balance_sheet": balance_sheet, "worry_map": worry_map,
           "fine_print": fine_print, "peer_compare": peer_compare}


# ---------- cache + run ----------

def cached(symbol: str, recipe: str) -> dict | None:
    from ..database import read_session_run

    def q(s):
        row = s.execute(select(Report).join(Company, Company.id == Report.company_id)
                        .where(Company.nse_symbol == symbol.upper(), Report.kind == f"deep:{recipe}")
                        .order_by(Report.created_at.desc()).limit(1)).scalar()
        if not row or datetime.now(timezone.utc) - row.created_at > TTL:
            return None
        return {"recipe": recipe, "title": TITLES[recipe], "symbol": symbol.upper(), **row.data, "receipt": row.receipt,
                "cached": True}
    return read_session_run(q)


def run(symbol: str, recipe: str, force: bool = False) -> dict:
    if recipe not in RUNNERS:
        raise ValueError(f"unknown deep dive {recipe}")
    if not force and (hit := cached(symbol, recipe)):
        return hit
    c = company(symbol)
    budget = f"deep:{symbol}:{recipe}:{int(time.time())}"
    t0 = time.monotonic()
    data = RUNNERS[recipe](c, budget)
    with session() as s:
        calls = s.scalars(select(LLMCall).where(LLMCall.budget_key == budget)).all()
    receipt = {"seconds": round(time.monotonic() - t0, 1), "llm_calls": sum(1 for x in calls if x.ok and not x.cache_hit),
               "cache_hits": sum(1 for x in calls if x.cache_hit), "cost_usd": round(sum(x.cost_usd for x in calls), 4),
               "models": sorted({x.model for x in calls if x.ok}), "generated_at": datetime.now(timezone.utc).isoformat()}
    with session() as s:
        s.add(Report(company_id=c.id, kind=f"deep:{recipe}", data=json.loads(json.dumps(data, default=str)), receipt=receipt))
    return {"recipe": recipe, "title": TITLES[recipe], "symbol": symbol.upper(), **json.loads(json.dumps(data, default=str)),
            "receipt": receipt, "cached": False}


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sym, rec = sys.argv[1], sys.argv[2]
    out = run(sym, rec, force="--force" in sys.argv)
    print(json.dumps(out, indent=1, default=str)[:4000])
