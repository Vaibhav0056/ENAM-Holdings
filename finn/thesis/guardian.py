"""Thesis Guardian (spec P5, R6).

- draft(): a thesis per holding, "Draft from management's claims": statement, 2-4 checkable KPIs, 2-3 breakers, built
  only from management's verified statements (annual-report strategy/segments/risks + the promise ledger). KPI
  thresholds must be numbers present in the evidence; anything else is dropped. A user-edited thesis is never replaced.
- impacts(): each attention / worth-a-look card judged against its holding's thesis (supports | watch | threatens |
  none) with a one-line "why it matters", batched on the cheap tier. Cards that touch the thesis get the 1.3 multiplier.
- statuses(): Intact / Watch / At risk from KPI checks on verified results cards and breaker events, with a reason
  and the triggering filing, stored as a timeline in thesis_status.
- results_day(): a results card compared with management's own prior guidance for that period from the ledger.

    python -m finn.thesis.guardian draft|impacts|status|results_day|all
"""
from __future__ import annotations

import json
import logging
import re
import sys
from typing import Literal, Optional

from pydantic import BaseModel
from sqlalchemy import select

from .. import guards
from ..database import session
from ..llm import router
from ..models import Company, Document, Enrichment, Filing, Thesis, ThesisStatus
from . import kpi as K

log = logging.getLogger("finn.thesis")
IMPACT_BATCH = 10
THESIS_MULTIPLIER = 1.3


class CitedText(BaseModel):
    text: str
    evidence: list[str]


class KPI(BaseModel):
    metric: Literal["revenue_growth_yoy", "ebitda_margin", "pat_growth_yoy", "pat_margin"]
    op: Literal[">=", "<="]
    threshold: float
    why: str
    evidence: list[str]


class ThesisDraft(BaseModel):
    statement: CitedText
    kpis: list[KPI]
    breakers: list[CitedText]


class Impact(BaseModel):
    id: int
    impact: Literal["supports", "watch", "threatens", "none"]
    why: Optional[str] = None


class ImpactBatch(BaseModel):
    items: list[Impact]


# ---------- evidence ----------

def evidence_for(company: Company) -> dict | None:
    """Management's own verified statements: the latest research report if one exists, else the latest annual
    report's facts (fetched and extracted on demand, cached)."""
    from ..research.engine import cached_report, extract_ar
    from ..sources.common import PoliteHTTP
    from ..sources.pdfstore import fetch_document
    items: list[dict] = []
    rep = cached_report(company.nse_symbol)
    if rep:
        snap = rep["snapshot"]
        snap_items = ([snap["what_it_does"]] if snap.get("what_it_does") else []) + snap.get("segments", []) + \
            snap.get("strategy", []) + snap.get("risks", [])
        for s in snap_items:
            items.append({"id": s["id"], "kind": "statement", "text": s.get("text") or f"{s.get('name')}: {s.get('description')}",
                          "quote": s.get("quote"), "page": s.get("page"), "doc": s.get("doc")})
        from ..research.ledger import unit_key
        # every ratio-like target (the only kind a KPI can use), then the rest, capped
        ledger = sorted(rep["ledger"], key=lambda g: unit_key(g.get("unit")) != "%")[:40]
        for p in ledger:
            items.append({"id": p["id"], "kind": "promise", "text": f"{p['metric']}: {p['target_text']} ({p['horizon']})",
                          "verdict": p["verdict"], "quote": p["quote"], "page": p["page"], "doc": p["doc"]})
        for a in rep["actuals"][:15]:
            items.append({"id": a["id"], "kind": "reported", "text": f"{a['metric']} {a['value']} {a['unit']} ({a['period']})",
                          "quote": a["quote"], "page": a["page"], "doc": a["doc"]})
        return {"source": f"research report {rep['report_id']}", "items": items}
    with session() as s:
        ar = s.scalar(select(Document).where(Document.company_id == company.id, Document.kind == "annual_report")
                      .order_by(Document.id).limit(1))
        doc = {"id": ar.id, "kind": ar.kind, "period": ar.period, "title": ar.title, "url": ar.url} if ar else None
    if not doc:
        return None
    if fetch_document(doc["id"], PoliteHTTP(1.0))["status"] != "parsed":
        return None
    f = extract_ar(doc, f"thesis:{company.nse_symbol}")
    if not f.get("ok"):
        return None
    n = 0
    for key in ("what_it_does", "segments", "strategy", "risks", "actuals"):
        vals = f.get(key) or []
        for v in ([vals] if isinstance(vals, dict) else vals):
            if not v:
                continue
            n += 1
            text = v.get("text") or (f"{v.get('name')}: {v.get('description')}" if v.get("name") else
                                     f"{v.get('metric')} {v.get('value')} {v.get('unit')} ({v.get('period')})")
            items.append({"id": f"S{n}", "kind": key, "text": text, "quote": v.get("quote"), "page": v.get("page"),
                          "doc": f"annual report {doc['period']}", "document_id": doc["id"]})
    return {"source": f"annual report {doc['period']} (document {doc['id']})", "items": items}


# ---------- drafting ----------

def draft(company: Company, force: bool = False) -> dict:
    with session() as s:
        existing = s.get(Thesis, company.id)
        if existing and existing.source == "user":
            return {"symbol": company.nse_symbol, "skipped": "user thesis"}
        if existing and not force:
            return {"symbol": company.nse_symbol, "skipped": "already drafted"}
    ev = evidence_for(company)
    if not ev or not ev["items"]:
        return {"symbol": company.nse_symbol, "skipped": "no verified management statements available"}
    slim = [{k: v for k, v in it.items() if k in ("id", "kind", "text", "verdict")} for it in ev["items"]]
    r = router.call("thesis_draft", "thesis_draft", {"company": company.name, "evidence": json.dumps(slim, ensure_ascii=False)},
                    ThesisDraft, budget_key=f"thesis:{company.nse_symbol}")
    ids = {it["id"] for it in ev["items"]}
    by_id_text = {it["id"]: it.get("text") or "" for it in ev["items"]}
    by_id_kind = {it["id"]: it.get("kind") for it in ev["items"]}
    allowed = guards.allowed_numbers(json.dumps(slim, ensure_ascii=False))
    removed = 0

    def clean(c: CitedText) -> dict | None:
        nonlocal removed
        raw = re.sub(r"\s*[\[(]\s*(?:[A-Z]\d+\s*,?\s*)+[\])]", "", c.text).strip()   # ids belong in evidence
        text, n, a = guards.scrub(raw, allowed)
        removed += n + a
        cites = [e for e in c.evidence if e in ids]
        return {"text": text, "evidence": cites} if text and cites else None

    statement = clean(r.data.statement)
    breakers = [b for b in (clean(b) for b in r.data.breakers) if b]
    kpis = []
    seen_metrics = set()
    seg = re.compile(r"\bsegment\b|\bdivision\b|\bbusiness\b|\bvertical\b", re.I)
    for k in r.data.kpis:
        cites = [e for e in k.evidence if e in ids]
        # company-wide only: a KPI resting solely on segment statements would be checked against the wrong number
        # A bar must come from a target management set, not from last year's actual (a peak sets up a false breach;
        # the model ignored this rule in the prompt for 3 of 5 drafts, so it is enforced here).
        target_backed = any(by_id_kind.get(e) in ("promise", "statement", "strategy") for e in cites)
        if not target_backed:
            removed += 1
            continue
        segment_only = cites and all(seg.search(by_id_text.get(e, "")) or _names_segment(by_id_text.get(e, ""), k.why) for e in cites)
        if cites and k.metric not in seen_metrics and not segment_only and guards.number_ok(round(k.threshold, 6), allowed):
            seen_metrics.add(k.metric)
            kpis.append({"metric": k.metric, "op": k.op, "threshold": k.threshold, "why": k.why, "evidence": cites})
        else:
            removed += 1
    if not statement:
        return {"symbol": company.nse_symbol, "skipped": "draft failed verification"}
    by_id = {it["id"]: it for it in ev["items"]}
    used = sorted({e for x in [statement] + breakers + kpis for e in x["evidence"]})
    citations = [{k: by_id[e].get(k) for k in ("id", "kind", "text", "quote", "page", "doc", "document_id")} for e in used]
    with session() as s:
        t = s.get(Thesis, company.id) or Thesis(company_id=company.id)
        t.statement, t.kpis, t.breakers, t.source = statement["text"], kpis, breakers, "draft"
        t.citations = {"statement_evidence": statement["evidence"], "items": citations, "evidence_source": ev["source"],
                       "model": r.model, "removed": removed, "label": "Draft from management's claims"}
        s.merge(t)
    return {"symbol": company.nse_symbol, "kpis": len(kpis), "breakers": len(breakers), "removed": removed, "model": r.model}


def _names_segment(evidence_text: str, why: str) -> bool:
    """True when the KPI's own words or its evidence name a segment (an all-caps acronym like FMEG or a
    'W&C'-style abbreviation) rather than the company as a whole."""
    text = f"{evidence_text} {why}"
    acronyms = set(re.findall(r"\b[A-Z]{3,6}\b", text)) - FINANCIAL_ACRONYMS
    named = bool(acronyms) or bool(re.search(r"\b[A-Z]&[A-Z]\b", text))
    return named and not re.search(r"consolidated|company|overall|group", text, re.I)


FINANCIAL_ACRONYMS = {"EBITDA", "EBIT", "PAT", "PBT", "YOY", "QOQ", "CAGR", "ROCE", "ROE", "ROA", "EPS", "NPA", "GNPA",
                      "NIM", "AUM", "INR", "USD", "CAPEX", "OPEX", "FY"}


def draft_all(force: bool = False) -> list[dict]:
    with session() as s:
        cos = list(s.scalars(select(Company).where(Company.is_portfolio.is_(True)).order_by(Company.nse_symbol)))
    out = []
    for c in cos:
        try:
            out.append(draft(c, force))
        except Exception as e:  # noqa: BLE001 - one holding never stops the others
            log.warning("thesis draft failed for %s: %s", c.nse_symbol, e)
            out.append({"symbol": c.nse_symbol, "error": str(e)[:200]})
    return out


# ---------- card impact ----------

def _card_text(f: Filing, e: Enrichment) -> str:
    p = e.payload or {}
    bits = [f"Headline: {e.headline}", f"Type: {e.type}"]
    if e.one_liner:
        bits.append(f"Summary: {e.one_liner}")
    if p.get("kind") == "results" and p.get("ok"):
        bits.append("Results: " + "; ".join(f"{r['metric']} {r['current']} cr, QoQ {r['qoq']}%, YoY {r['yoy']}%"
                                             for r in p["table"] if r.get("found")))
    if p.get("people"):
        bits.append("People: " + "; ".join(f"{x['name']} ({x.get('role')}) {x['direction']}" for x in p["people"][:3]))
    for fl in (p.get("flags") or []) + (p.get("governance_flags") or []):
        bits.append(f"Flag: {fl['kind']} ({fl['severity']})")
    bits.append(f"Filing text: {(f.body or f.subject)[:400]}")
    return "\n".join(bits)


def _thesis_text(t: Thesis) -> str:
    k = "; ".join(f"{x['metric']} {x['op']} {x['threshold']}%" for x in (t.kpis or []))
    b = "; ".join(x["text"] for x in (t.breakers or []))
    return f"Thesis: {t.statement}\nKPIs: {k or 'none'}\nBreakers: {b or 'none'}"


def impacts(limit: int | None = None, use_llm: bool = True) -> dict:
    with session() as s:
        theses = {t.company_id: t for t in s.scalars(select(Thesis))}
        rows = s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
                         .where(Enrichment.bucket.in_(["attention", "worth_a_look"]), Enrichment.thesis_impact.is_(None))
                         .order_by(Filing.filed_at.desc())).all()
        todo = [(f.id, _thesis_text(theses[f.company_id]), _card_text(f, e)) for f, e in rows if f.company_id in theses]
    if limit:
        todo = todo[:limit]
    done = {"supports": 0, "watch": 0, "threatens": 0, "none": 0, "failed": 0}
    for i in range(0, len(todo), IMPACT_BATCH):
        chunk = todo[i:i + IMPACT_BATCH]
        if not use_llm:
            break
        doc = "\n\n".join(f"[ID {fid}]\n{th}\n---\n{card}" for fid, th, card in chunk)
        try:
            r = router.call("thesis_impact", "thesis_impact", {"document": doc}, ImpactBatch)
        except Exception as e:  # noqa: BLE001
            log.warning("impact batch failed: %s", e)
            done["failed"] += len(chunk)
            continue
        ids = {fid for fid, _, _ in chunk}
        with session() as s:
            for it in r.data.items:
                if it.id not in ids:
                    continue
                e = s.get(Enrichment, it.id)
                why = None
                if it.impact != "none" and it.why:
                    why, _, adv = guards.scrub(it.why, guards.allowed_numbers(dict(chunk_text=[c for c in chunk if c[0] == it.id])))
                    why = why or None
                impact = it.impact
                if impact == "none":
                    floor = structural_impact(e, s.get(Filing, it.id))
                    if floor:
                        impact, why = floor, why or _structural_why(e)
                e.thesis_impact, e.why_it_matters = impact, why
                done[impact] += 1
    return done


STRUCTURAL_RE = re.compile(r"complet\w* (the )?acquisition|acquired|insolvency|nclt|nclat|show cause|penalt|warning letter", re.I)


def structural_impact(e: Enrichment, f: Filing) -> str | None:
    """For an investor who owns a business for its management and its numbers, some events bear on every thesis no
    matter what the thesis says: results, a CEO/CFO/MD or compliance-head change, a capital raise, high-severity
    governance flags, a completed acquisition, or legal action against the company. Floor: "watch"."""
    p = e.payload or {}
    if e.type == "results":
        return "watch"
    if e.type == "fundraise":
        return "watch"
    if e.type == "governance" or any(fl.get("severity") == "high" for fl in (p.get("flags") or []) + (p.get("governance_flags") or [])):
        return "watch"
    if e.type == "mgmt_change" and (any(x.get("senior") for x in p.get("people") or []) or
                                    re.search(r"compliance officer|chief executive|managing director|chief financial", e.headline or "", re.I)):
        return "watch"
    if e.type in ("m_and_a", "litigation_regulatory") and STRUCTURAL_RE.search(f"{e.headline} {f.body or ''} {e.one_liner or ''}"):
        return "watch"
    return None


def _structural_why(e: Enrichment) -> str:
    return {"results": "Quarterly results: check them against the thesis.",
            "fundraise": "Capital raise: dilution and use of funds bear on the thesis.",
            "mgmt_change": "Senior leadership change: management quality is part of every thesis.",
            "governance": "Governance flag: integrity is part of every thesis."}.get(e.type, "Bears on the thesis: review the filing.")


def multiplier(impact: str | None) -> tuple[float, str] | None:
    """Only cards that challenge the thesis are pushed up. Confirmation ("supports") is shown on the card but doesn't
    earn a place in Needs attention: with it, attention grew from 101 to 128 cards and only a third were material."""
    if impact in ("watch", "threatens"):
        return THESIS_MULTIPLIER, f"touches your thesis ({impact})"
    return None


# ---------- status engine ----------

# Integrity red flags break any thesis for an investor who owns a business for its management. A senior exit is
# worth watching (planned successions and superannuation are common) but does not break a thesis by itself.
BREAKER_FLAG_KINDS = {"auditor_resignation", "fraud", "default", "regulatory_action", "modified_opinion"}
WATCH_FLAG_KINDS = {"senior_exit", "independent_exit", "insolvency", "pledge_change"}


def statuses() -> dict:
    out = {}
    with session() as s:
        theses = {t.company_id: t for t in s.scalars(select(Thesis))}
        cos = {c.id: c for c in s.scalars(select(Company).where(Company.id.in_(list(theses) or [-1])))}
        for cid, t in theses.items():
            cards = s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
                              .where(Filing.company_id == cid).order_by(Filing.filed_at)).all()
            history, events, watch, last_results = [], [], [], None
            for f, e in cards:
                p = e.payload or {}
                if e.type == "results" and p.get("ok"):
                    checks = K.check(t.kpis or [], K.kpi_values(p))
                    if checks:
                        history.append(checks)
                        last_results = f.id
                flags = [fl for fl in (p.get("flags") or []) + (p.get("governance_flags") or [])
                         if fl.get("kind") in BREAKER_FLAG_KINDS and fl.get("severity") == "high"]
                if e.thesis_impact == "threatens" or flags:
                    events.append({"summary": (flags[0]["summary"] if flags else e.why_it_matters or e.headline)[:200],
                                   "filing_id": f.id})
                wflags = [fl for fl in (p.get("flags") or []) + (p.get("governance_flags") or []) if fl.get("kind") in WATCH_FLAG_KINDS]
                if wflags:
                    watch.append({"summary": f"{wflags[0]['kind'].replace('_', ' ')}: {wflags[0]['summary']}"[:200], "filing_id": f.id})
            st, reason = K.status(history, events, watch)
            if st == "watch" and watch and not any(c["breached"] for c in (history[-1] if history else [])):
                trigger_watch = watch[-1]["filing_id"]
            else:
                trigger_watch = None
            trigger = events[-1]["filing_id"] if st == "at_risk" and events else (trigger_watch or last_results)
            prev = s.scalar(select(ThesisStatus).where(ThesisStatus.company_id == cid).order_by(ThesisStatus.at.desc()).limit(1))
            if not prev or prev.status != st or prev.reason != reason:
                s.add(ThesisStatus(company_id=cid, status=st, reason=reason, filing_id=trigger))
            out[cos[cid].nse_symbol] = {"status": st, "reason": reason}
    return out


# ---------- results-day verdict ----------

RESULT_METRICS = {"revenue_growth_yoy": re.compile(r"revenue|sales|top ?line", re.I),
                  "ebitda_margin": re.compile(r"ebitda margin|operating margin", re.I),
                  "pat_growth_yoy": re.compile(r"(pat|profit|earnings).{0,10}growth", re.I)}


def results_day() -> int:
    """Attach to each verified results card the comparison with management's own prior guidance for that period."""
    from ..research import ledger as L
    from ..research.engine import cached_report
    n = 0
    with session() as s:
        rows = s.execute(select(Filing, Enrichment, Company).join(Enrichment, Enrichment.filing_id == Filing.id)
                         .join(Company, Company.id == Filing.company_id).where(Enrichment.type == "results")).all()
        for f, e, c in rows:
            p = e.payload or {}
            if not p.get("ok") or not p.get("period"):
                continue
            rep = cached_report(c.nse_symbol)
            if not rep:
                continue
            q = L.period(p["period"])
            vals = K.kpi_values(p)
            comps = []
            for key, rx in RESULT_METRICS.items():
                if key not in vals:
                    continue
                if key.endswith("growth_yoy"):
                    pass
                for g in rep["ledger"]:
                    if not rx.search(g["metric"]) or L.unit_key(g.get("unit")) != "%" or (g["target_low"] is None and g["target_high"] is None):
                        continue
                    if key == "revenue_growth_yoy" and "growth" not in g["metric"].lower():
                        continue
                    hz = L.period(g.get("horizon"))
                    # a quarter's ratio can be compared with that quarter's or that year's guided ratio
                    if hz is None or not (hz == q or (hz.kind == "year" and hz.fy == q.fy)):
                        continue
                    v = L.verdict_for(dict(g, horizon=p["period"]), {"value": vals[key], "unit": "%"}, q)
                    comps.append({"metric": key, "promised": g["target_text"], "said_in": g["said_in"], "quote": g["quote"],
                                  "page": g["page"], "doc": g["doc"], "delivered": vals[key], "verdict": v,
                                  "promise_id": g["id"]})
                    break
            # Second source: promises the ledger already tracks against figures management reported for this quarter
            # (AUM growth, ROA, segment margins...), which the P&L table itself doesn't carry.
            seen = {c["promise_id"] for c in comps}
            for g in rep["ledger"]:
                lt = g.get("latest") or {}
                if g["id"] in seen or not g.get("tracking") or L.period(lt.get("period")) not in (q, L.period(f"H{1 if q.q in (1, 2) else 2} FY{q.fy % 100:02d}")):
                    continue
                comps.append({"metric": g["metric"], "promised": g["target_text"], "horizon": g["horizon"],
                              "said_in": g["said_in"], "quote": g["quote"], "page": g["page"], "doc": g["doc"],
                              "delivered": lt.get("value"), "delivered_unit": lt.get("unit"), "delivered_period": lt.get("period"),
                              "delivered_quote": lt.get("quote"), "verdict": g["tracking"], "promise_id": g["id"]})
                if len(comps) >= 5:
                    break
            if not comps:
                continue
            misses_4q = sum(1 for g in rep["ledger"] if g["verdict"] == "missed")
            p = dict(p, results_day={"comparisons": comps, "missed_in_ledger": misses_4q, "report_id": rep["report_id"]})
            e.payload = p
            n += 1
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("draft", "all"):
        for r in draft_all(force="--force" in sys.argv):
            print("draft", r)
    if what in ("impacts", "all"):
        print("impacts", impacts())
    if what in ("results_day", "all"):
        print("results_day", results_day())
    if what in ("status", "all"):
        print("status", json.dumps(statuses(), indent=1))
