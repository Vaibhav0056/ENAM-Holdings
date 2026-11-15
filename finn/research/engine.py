"""Research engine (Pull) for any NSE ticker: R1 fetch → R2 extract → R3 verify + match → R4 report.

    python -m finn.research.engine POLYCAB [--force]

- R1: up to 8 transcripts + 2 annual reports from screener.in's Documents (primary PDFs on BSE or the company site),
  fetched in parallel through the polite PDF store, cached by sha256.
- R2: specialist extractors on the cheap tier, in parallel (bounded): promises + reported actuals per transcript
  (management remarks only), business facts + strategy + risks per annual report.
- R3: every quote verified on its page (±1, rapidfuzz ≥ 90); figures must be printed in their own quote; promises must
  come from a management speaker. Matching, verdicts, credibility, vagueness and drift are Python (finn.research.ledger).
- R4: one strong-tier synthesis (credibility read, bull, bear) and one devil's advocate over the verified JSON only;
  sentences with unsupported numbers or advice language are removed. A receipt records sources, time, calls and cost.
Progress is written to research_jobs at each stage (polled or streamed over SSE). Reports are cached for REPORT_TTL (7 days).
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel
from sqlalchemy import delete, func, select

from .. import guards
from ..database import session
from ..grounding import THRESHOLD, value_in_quote, verify
from ..llm import router
from ..models import Actual, Company, Document, Extraction, LedgerRow, LLMCall, Page, Promise, Report, ResearchJob
from ..sources import screener
from ..sources.common import PoliteHTTP, insert_ignore
from ..sources.pdfstore import fetch_document
from . import ledger as L
from .transcripts import is_management, management_text, split_turns

log = logging.getLogger("finn.research")
STAGES = ["Finding documents", "Fetching documents", "Reading transcripts", "Extracting promises and facts",
          "Verifying quotes", "Matching promises to results", "Writing the verdict", "Checking numbers"]
# Reports read transcripts and annual reports, which change quarterly; finn.precompute rebuilds them weekly.
REPORT_TTL = timedelta(days=float(__import__('os').getenv('REPORT_TTL_DAYS', '7')))
MAX_TRANSCRIPTS = 8
MAX_ARS = 2
CONCURRENCY = 6
PROMPT_VERSIONS = {"promises": "promise_extract", "facts": "facts_extract"}

# ---------- schemas ----------


class PromiseItem(BaseModel):
    metric: str
    target_text: str
    target_low: Optional[float] = None
    target_high: Optional[float] = None
    unit: str
    horizon: str
    speaker: str
    quote: str
    page: int


class ActualItem(BaseModel):
    metric: str
    period: str
    value: float
    unit: str
    quote: str
    page: int


class CallPromises(BaseModel):
    quarter: str
    promises: list[PromiseItem]


class CallActuals(BaseModel):
    actuals: list[ActualItem]


class Cited(BaseModel):
    text: str
    quote: str
    page: int


class Segment(BaseModel):
    name: str
    description: str
    share_pct: Optional[float] = None
    quote: str
    page: int


class Facts(BaseModel):
    what_it_does: Optional[Cited] = None
    segments: list[Segment] = []
    revenue_mix: list[Cited] = []
    actuals: list[ActualItem] = []
    strategy: list[Cited] = []
    risks: list[Cited] = []


class Bullet(BaseModel):
    text: str
    evidence: list[str]


class Synthesis(BaseModel):
    credibility: list[Bullet]
    bull: list[Bullet]
    bear: list[Bullet]


class Devil(BaseModel):
    points: list[Bullet]


# ---------- jobs ----------

def _job(job_id: str, **kw):
    with session() as s:
        j = s.get(ResearchJob, job_id)
        for k, v in kw.items():
            setattr(j, k, v)


_timings: dict[str, dict] = {}


def _stage(job_id: str, i: int, frac: float = 0.0):
    t = _timings.setdefault(job_id, {"_t0": time.monotonic(), "_last": None})
    now = time.monotonic()
    if t["_last"] is not None and frac == 0.0:
        name, since = t["_last"]
        t[name] = round(now - since, 1)
    if frac == 0.0:
        t["_last"] = (STAGES[min(i, len(STAGES) - 1)], now)
    _job(job_id, stage=STAGES[min(i, len(STAGES) - 1)], progress=round((i + frac) / len(STAGES), 3))


# ---------- R1: documents ----------

def resolve_company(symbol: str, http: PoliteHTTP) -> tuple[int, str, dict | None]:
    """Existing company, or a new research-only one for any NSE ticker screener.in knows."""
    with session() as s:
        c = s.scalar(select(Company).where(Company.nse_symbol == symbol))
        if c:
            fresh = s.scalar(select(func.max(Document.id)).where(Document.company_id == c.id,
                                                                Document.kind.in_(["transcript", "annual_report"])))
            return c.id, c.name, None if fresh else {}
    found = screener.list_documents(symbol, http, max_transcripts=MAX_TRANSCRIPTS, max_ars=MAX_ARS)
    with session() as s:
        c = Company(nse_symbol=symbol, name=found["name"] or symbol, yahoo_symbol=f"{symbol}.NS", is_portfolio=False,
                    note="added by research (not in the portfolio)")
        s.add(c)
        s.flush()
        cid = c.id
    return cid, found["name"] or symbol, found


def list_documents(cid: int, symbol: str, found: dict | None, http: PoliteHTTP) -> list[dict]:
    if found is None or found == {}:
        found = screener.list_documents(symbol, http, max_transcripts=MAX_TRANSCRIPTS, max_ars=MAX_ARS) \
            if found == {} else None
    if found:
        rows = [{"company_id": cid, "kind": d["kind"], "period": d["period"], "title": d["title"], "url": d["url"],
                 "status": "listed"} for d in found["transcripts"] + found["annual_reports"] + found.get("overrides", [])]
        insert_ignore(Document, rows, ["company_id", "url"])
    with session() as s:
        docs = s.scalars(select(Document).where(Document.company_id == cid, Document.kind.in_(["transcript", "annual_report"]))
                         .order_by(Document.id)).all()
        tr = [d for d in docs if d.kind == "transcript"][:MAX_TRANSCRIPTS]
        ar = [d for d in docs if d.kind == "annual_report"][:MAX_ARS]
        return [{"id": d.id, "kind": d.kind, "period": d.period, "title": d.title, "url": d.url} for d in tr + ar]


def pages_of(doc_id: int) -> list[str]:
    with session() as s:
        return [t for _, t in s.execute(select(Page.page_no, Page.text).where(Page.document_id == doc_id)
                                        .order_by(Page.page_no)).all()]


AR_KEYWORDS = ["segment", "revenue", "business", "we are", "our company", "ebitda", "margin", "growth", "highlights",
               "key performance", "financial performance", "overview", "capex", "products", "customers", "strategy",
               "risk", "outlook", "chairman", "managing director"]


def ar_pages(pages: list[str], k: int = 12) -> list[int]:
    scored = []
    for i, t in enumerate(pages[:240], 1):
        low = t.lower()
        if len(low) < 400:
            continue
        s = sum(low.count(w) for w in AR_KEYWORDS)
        if "notes to" in low or "independent auditor" in low or "form no. mgt" in low or "annexure" in low[:300]:
            s *= 0.2
        scored.append((s, i))
    return sorted(i for _, i in sorted(scored, reverse=True)[:k])


# ---------- R2 + R3: extraction with verification ----------

def _cached_extraction(doc_id: int, schema: str, version: str) -> dict | None:
    with session() as s:
        e = s.scalar(select(Extraction).where(Extraction.document_id == doc_id, Extraction.schema == schema,
                                              Extraction.prompt_version == version))
        return e.data if e else None


def _save_extraction(doc_id: int, schema: str, version: str, data: dict, ratio: float | None):
    insert_ignore(Extraction, [{"document_id": doc_id, "schema": schema, "prompt_version": version, "data": data,
                                "verified_ratio": ratio}], ["document_id", "schema", "prompt_version"])


def extract_call(doc: dict, budget_key: str) -> dict:
    """One transcript -> verified promises and actuals, from two specialist calls (promises, actuals) over management
    remarks only. One combined call let actuals crowd out promises on long calls. Cached per (document, versions)."""
    pv, av = router.load_prompt("promise_extract")[0], router.load_prompt("actuals_extract")[0]
    version = f"{pv}+{av}"
    hit = _cached_extraction(doc["id"], "promises", version)
    if hit is not None:
        return {**hit, "cached": True}
    pages = pages_of(doc["id"])
    turns = split_turns(pages)
    text = management_text(turns)
    if not text:
        out = {"ok": False, "reason": "no management remarks found (scanned or unusual format)"}
        _save_extraction(doc["id"], "promises", version, out, None)
        return out
    label = doc.get("period") or doc.get("title")
    rp = router.call("promise_extract", "promise_extract", {"call_label": label, "document": text}, CallPromises,
                     budget_key=budget_key)
    ra = router.call("actuals_extract", "actuals_extract", {"call_label": label, "document": text}, CallActuals,
                     budget_key=budget_key)
    raw_p = [dict(p.model_dump(), doc_id=doc["id"]) for p in rp.data.promises]
    raw_a = [dict(a.model_dump(), doc_id=doc["id"]) for a in ra.data.actuals]
    n_raw = len(raw_p) + len(raw_a)
    prom = [p for p in verify(raw_p, pages) if is_management(p["speaker"], turns)]
    # The call's own date is reliable (a May call covers Q4); the model's reading of the quarter is the fallback.
    said = doc.get("period") or rp.data.quarter
    for p in prom:
        for k in ("target_low", "target_high"):   # a bound the quote doesn't print can't be judged against
            if not value_in_quote(p[k], p["quote"]):
                p[k] = None
        p["said_in"] = said
    acts = [a for a in verify(raw_a, pages) if value_in_quote(a["value"], a["quote"])]
    kept = len(prom) + len(acts)
    out = {"ok": True, "quarter": said, "model_quarter": rp.data.quarter, "promises": prom, "actuals": acts,
           "extracted": n_raw, "kept": kept, "dropped": n_raw - kept, "pages": len(pages),
           "management_turns": sum(t["role"] == "management" for t in turns), "model": rp.model}
    _save_extraction(doc["id"], "promises", version, out, kept / n_raw if n_raw else None)
    return out


def extract_ar(doc: dict, budget_key: str) -> dict:
    version = router.load_prompt("facts_extract")[0]
    hit = _cached_extraction(doc["id"], "facts", version)
    if hit is not None:
        return {**hit, "cached": True}
    pages = pages_of(doc["id"])
    sel = ar_pages(pages)
    if not sel:
        return {"ok": False, "reason": "no readable pages"}
    text = "\n\n".join(f"[PAGE {p}]\n{pages[p - 1][:6000]}" for p in sel)
    r = router.call("facts_extract", "facts_extract", {"report_label": doc.get("title") or doc.get("period"),
                                                       "document": text}, Facts, budget_key=budget_key)
    f = r.data
    def ver(items):
        return verify([dict(i.model_dump(), doc_id=doc["id"]) for i in items], pages)
    out = {"ok": True, "period": doc.get("period"),
           "what_it_does": (ver([f.what_it_does]) or [None])[0] if f.what_it_does else None,
           "segments": ver(f.segments), "revenue_mix": ver(f.revenue_mix), "strategy": ver(f.strategy),
           "risks": ver(f.risks), "actuals": [a for a in ver(f.actuals) if value_in_quote(a["value"], a["quote"])],
           "pages": len(pages), "pages_read": sel, "model": r.model}
    n_raw = (1 if f.what_it_does else 0) + sum(len(x) for x in (f.segments, f.revenue_mix, f.strategy, f.risks, f.actuals))
    kept = (1 if out["what_it_does"] else 0) + sum(len(out[k]) for k in ("segments", "revenue_mix", "strategy", "risks", "actuals"))
    out.update(extracted=n_raw, kept=kept, dropped=n_raw - kept)
    _save_extraction(doc["id"], "facts", version, out, kept / n_raw if n_raw else None)
    return out


# ---------- R4: synthesis with guards ----------

def _clean_bullets(items: list[dict], ids: set[str], allowed: set[float], limit: int) -> tuple[list[dict], int, int]:
    out, bad_num, bad_adv = [], 0, 0
    for b in items[:limit]:
        text, n, a = guards.scrub(b["text"], allowed)
        bad_num += n
        bad_adv += a
        ev = [e for e in b["evidence"] if e in ids]
        if text and ev:
            out.append({"text": text, "evidence": ev})
    return out, bad_num, bad_adv


MAX_EVIDENCE_CHARS = 24_000      # ~6k tokens: enough for every measured promise plus the most recent others
STEP_BUDGET_S = 75.0             # one synthesis step's wall-clock budget across retries and fallbacks


def cap_evidence(evidence: dict, limit: int = MAX_EVIDENCE_CHARS) -> tuple[dict, int]:
    """Verified evidence for synthesis, trimmed to `limit` characters of JSON. Keeps every measured promise
    (delivered / partial / missed) first, then pending and unverifiable ones, newest first; then actuals, drift and
    the snapshot. Returns the capped evidence and how many items were left out (stated in the receipt)."""
    ev = {"credibility": evidence["credibility"], "promises": [], "actuals": [], "drift": [], "snapshot": []}
    rank = {"delivered": 0, "partial": 0, "missed": 0, "pending": 1, "unverifiable": 2}
    queue = [("promises", p) for p in sorted(evidence["promises"], key=lambda p: rank.get(p["verdict"], 3))]
    queue += [("drift", d) for d in evidence["drift"]] + [("snapshot", s) for s in evidence["snapshot"]]
    queue += [("actuals", a) for a in evidence["actuals"]]
    size, left_out = len(json.dumps(ev, ensure_ascii=False, default=str)), 0
    for key, item in queue:
        n = len(json.dumps(item, ensure_ascii=False, default=str)) + 2
        if size + n > limit:
            left_out += 1
            continue
        ev[key].append(item)
        size += n
    return ev, left_out


def ledger_reading(cred: dict) -> str:
    """The ledger's own verdict, computed in Python: what the devil's advocate argues against. Independent of the
    synthesis call, so the two run in parallel."""
    c = cred.get("counts") or {}
    score = f"credibility {cred['score']}/100" if cred.get("score") is not None else "not scored (too few promises have matured)"
    return (f"{score}; {cred.get('measured', 0)} of {cred.get('total', 0)} promises measurable so far; delivered "
            f"{c.get('delivered', 0)}, partial {c.get('partial', 0)}, missed {c.get('missed', 0)}, pending {c.get('pending', 0)}, "
            f"too vague to check {c.get('unverifiable', 0)} ({cred.get('vagueness_pct', 0)}%).")


def synthesize(company: str, evidence: dict, budget_key: str, notes: list[str]) -> tuple[dict, dict]:
    ev, left_out = cap_evidence(evidence)
    ev_json = json.dumps(ev, ensure_ascii=False, default=str)
    allowed = guards.allowed_numbers(ev_json)
    ids = set(__import__("re").findall(r'"id": "([A-Z]\d+)"', ev_json))
    stats = {"removed_unsupported_numbers": 0, "removed_advice": 0, "removed_uncited": 0, "evidence_items_left_out": left_out,
             "evidence_chars": len(ev_json), "fallbacks": []}
    syn = {"credibility": [], "bull": [], "bear": [], "devils_advocate": []}
    models = []

    def main():
        return router.call("synthesis", "synthesis", {"company": company, "evidence": ev_json}, Synthesis,
                           budget_key=budget_key, deadline_s=STEP_BUDGET_S)

    def devil():
        return router.call("devils_advocate", "devils_advocate@v2",
                           {"company": company, "reading": ledger_reading(evidence["credibility"]), "evidence": ev_json},
                           Devil, budget_key=budget_key, deadline_s=STEP_BUDGET_S)

    with ThreadPoolExecutor(max_workers=2) as ex:      # the two sections are independent: run them together
        futs = {"synthesis": ex.submit(main), "devils_advocate": ex.submit(devil)}
        results = {}
        for name, f in futs.items():
            try:
                results[name] = f.result()
            except router.BudgetExceeded as e:
                notes.append(f"{name.replace('_', ' ').capitalize()} skipped: per-report budget reached ({e}).")
            except Exception as e:  # noqa: BLE001 - a missing section is stated, never faked
                notes.append(f"{'Synthesis' if name == 'synthesis' else 'Counter-argument'} unavailable: {str(e)[:160]}")
    for name, r in results.items():
        models.append(r.model)
        stats["downgraded"] = stats.get("downgraded") or bool(r.meta.get("downgraded"))
        if r.meta.get("fallback"):
            stats["fallbacks"].append({"step": name, "model": r.model, "reasons": r.meta.get("fallback_reasons") or []})
    if "synthesis" in results:
        r = results["synthesis"]
        for sec, lim in (("credibility", 4), ("bull", 3), ("bear", 3)):
            raw = [b.model_dump() for b in getattr(r.data, sec)]
            syn[sec], n, a = _clean_bullets(raw, ids, allowed, lim)
            stats["removed_unsupported_numbers"] += n
            stats["removed_advice"] += a
            stats["removed_uncited"] += len(raw[:lim]) - len(syn[sec])
    if "devils_advocate" in results:
        raw = [b.model_dump() for b in results["devils_advocate"].data.points]
        syn["devils_advocate"], n, a = _clean_bullets(raw, ids, allowed, 3)
        stats["removed_unsupported_numbers"] += n
        stats["removed_advice"] += a
    stats["models"] = models
    return syn, stats


# ---------- orchestration ----------

def _pool_map(fn, items, budget_key):
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(_safe, fn, it, budget_key) for it in items]
        return [f.result() for f in futs]


def _safe(fn, item, budget_key):
    try:
        return fn(item, budget_key)
    except Exception as e:  # noqa: BLE001 - one bad document never stops the report
        log.warning("%s failed for document %s: %s", fn.__name__, item.get("id"), e)
        return {"ok": False, "reason": str(e)[:300]}


def run(symbol: str, job_id: str) -> dict:
    t0 = time.monotonic()
    symbol = symbol.upper().strip()
    budget = f"research:{job_id}"
    http = PoliteHTTP(1.0)
    notes: list[str] = []
    _stage(job_id, 0)
    cid, name, found = resolve_company(symbol, http)
    _job(job_id, company_id=cid)
    docs = list_documents(cid, symbol, found, http)
    transcripts = [d for d in docs if d["kind"] == "transcript"]
    reports = [d for d in docs if d["kind"] == "annual_report"]
    if not transcripts:
        notes.append("No earnings-call transcripts are listed for this company; the report uses annual reports only "
                     "and cannot build a promise ledger.")

    _stage(job_id, 1)
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        fetched = list(ex.map(lambda d: fetch_document(d["id"], http), docs))
    status = {d["id"]: f for d, f in zip(docs, fetched)}

    _stage(job_id, 2)
    ok_tr = [d for d in transcripts if status[d["id"]]["status"] == "parsed"]
    ok_ar = [d for d in reports if status[d["id"]]["status"] == "parsed"]

    _stage(job_id, 3)
    calls = _pool_map(extract_call, ok_tr, budget)
    facts = _pool_map(extract_ar, ok_ar, budget)

    _stage(job_id, 4)
    sources, promises, actuals = [], [], []
    snapshot = {"what_it_does": None, "segments": [], "revenue_mix": [], "strategy": [], "risks": []}
    by_doc = {d["id"]: x for d, x in list(zip(ok_tr, calls)) + list(zip(ok_ar, facts))}
    for i, d in enumerate(docs, 1):
        d_id = f"D{i}"
        st = status[d["id"]]
        x = by_doc.get(d["id"], {})
        src = {"id": d_id, "document_id": d["id"], "kind": d["kind"], "title": d["title"], "period": d["period"],
               "url": d["url"], "status": st["status"] if st["status"] != "parsed" else ("read" if x.get("ok") else "not read"),
               "reason": st.get("error") or x.get("reason"), "pages": st.get("pages") or x.get("pages"),
               "extracted": x.get("extracted"), "kept": x.get("kept"), "dropped": x.get("dropped"),
               "from_cache": bool(x.get("cached"))}
        sources.append(src)
        if not x.get("ok"):
            continue
        for p in x.get("promises", []):
            promises.append(dict(p, doc=d_id))
        for a in x.get("actuals", []):
            actuals.append(dict(a, doc=d_id))
        if d["kind"] == "annual_report":
            if x.get("what_it_does") and not snapshot["what_it_does"]:
                snapshot["what_it_does"] = dict(x["what_it_does"], doc=d_id)
            for k in ("segments", "revenue_mix", "strategy", "risks"):
                if not snapshot[k]:   # latest annual report first
                    snapshot[k] = [dict(v, doc=d_id) for v in x.get(k, [])]

    _stage(job_id, 5)
    promises = L.dedupe(promises)
    for i, p in enumerate(promises, 1):
        p["id"] = f"G{i}"
    for i, a in enumerate(actuals, 1):
        a["id"] = f"A{i}"
    rows = L.match(promises, actuals)
    order = {v: i for i, v in enumerate(L.VERDICTS)}
    rows.sort(key=lambda r: (order[r["verdict"]], -(L.period(r["promise"].get("said_in")) or L.Period(0)).fy))
    cred = L.credibility(rows)
    drift = L.drift(promises)
    for i, d in enumerate(drift, 1):
        d["id"] = f"R{i}"
    snap_items = ([snapshot["what_it_does"]] if snapshot["what_it_does"] else []) + snapshot["segments"] + \
        snapshot["revenue_mix"] + snapshot["strategy"] + snapshot["risks"]
    for i, s in enumerate(snap_items, 1):
        s["id"] = f"S{i}"

    _stage(job_id, 6)
    evidence = {
        "credibility": {k: cred[k] for k in ("score", "measured", "total", "vagueness_pct", "counts")},
        "promises": [{"id": r["promise"]["id"], "metric": r["promise"]["metric"], "target": r["promise"]["target_text"],
                      "horizon": r["promise"]["horizon"], "said_in": r["promise"].get("said_in"), "verdict": r["verdict"],
                      "actual": (r["actual"] or {}).get("value"), "actual_id": (r["actual"] or {}).get("id"),
                      "latest": (r["progress"] or {}).get("value"), "tracking": r["tracking"]} for r in rows],
        "actuals": [{"id": a["id"], "metric": a["metric"], "period": a["period"], "value": a["value"], "unit": a["unit"]}
                    for a in actuals],
        "drift": [{"id": d["id"], "metric": d["metric"], "horizon": d["horizon"], "changes": [s["change"] for s in d["steps"]],
                   "dropped": d["dropped"]} for d in drift],
        "snapshot": [{"id": s["id"], "text": s.get("text") or f"{s.get('name')}: {s.get('description')}"} for s in snap_items],
    }
    synthesis, syn_stats = ({"credibility": [], "bull": [], "bear": [], "devils_advocate": []}, {"models": []})
    if promises or actuals or snap_items:
        synthesis, syn_stats = synthesize(name, evidence, budget, notes)

    _stage(job_id, 7)
    _persist_ledger(cid, promises, actuals, rows)
    with session() as s:
        calls_q = s.execute(select(LLMCall).where(LLMCall.budget_key == budget)).scalars().all()
    receipt = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - t0, 1),
        "stage_seconds": {k: v for k, v in _timings.pop(job_id, {}).items() if not k.startswith("_")},
        "sources_read": sum(1 for s in sources if s["status"] == "read"), "sources_listed": len(sources),
        "llm_calls": sum(1 for c in calls_q if not c.cache_hit and c.ok), "cache_hits": sum(1 for c in calls_q if c.cache_hit),
        "failed_calls": sum(1 for c in calls_q if not c.ok), "in_tokens": sum(c.in_tokens for c in calls_q),
        "out_tokens": sum(c.out_tokens for c in calls_q), "cost_usd": round(sum(c.cost_usd for c in calls_q), 4),
        "cost_cap_usd": router.config.MAX_COST_PER_REPORT_USD,
        "models": sorted({c.model for c in calls_q if c.ok and not c.cache_hit} | set(syn_stats.get("models", []))),
        "strong_tier_note": (("Synthesis fell back to the cheap tier: " + "; ".join(
                                 r for f in syn_stats.get("fallbacks", []) for r in f["reasons"]) + ".")
                             if syn_stats.get("downgraded") else
                             "No Pro-class model is available on this key; synthesis ran on the best available Flash model."),
        "synthesis_models": syn_stats.get("models", []),
        "synthesis_fallbacks": syn_stats.get("fallbacks", []),
        "synthesis_evidence_chars": syn_stats.get("evidence_chars"),
        "synthesis_evidence_left_out": syn_stats.get("evidence_items_left_out", 0),
        "quote_check": f"rapidfuzz partial_ratio ≥ {THRESHOLD} on the cited page ±1; figures must be printed in their quote",
        "facts_dropped_unverified": sum(s.get("dropped") or 0 for s in sources),
        "sentences_removed_unsupported_numbers": syn_stats.get("removed_unsupported_numbers", 0),
        "sentences_removed_advice": syn_stats.get("removed_advice", 0),
        "unverified_claims_shown": 0,
    }
    data = {
        "symbol": symbol, "name": name, "notes": notes, "sources": sources,
        "ledger": [{**{k: r["promise"].get(k) for k in ("id", "metric", "target_text", "target_low", "target_high", "unit",
                                                         "horizon", "said_in", "speaker", "quote", "page", "doc",
                                                         "also_said_in")},
                    "verdict": r["verdict"], "tracking": r["tracking"], "actual": r["actual"], "latest": r["progress"]}
                   for r in rows],
        "credibility": cred, "drift": drift, "snapshot": snapshot, "actuals": actuals, "synthesis": synthesis,
    }
    with session() as s:
        rep = Report(company_id=cid, kind="core", data=data, receipt=receipt)
        s.add(rep)
        s.flush()
        rid = rep.id
    _job(job_id, stage="Done", progress=1.0, status="done", report_id=rid)
    return {"report_id": rid, **data, "receipt": receipt}


def _persist_ledger(cid: int, promises: list[dict], actuals: list[dict], rows: list[dict]):
    """Normalised copies for cross-report queries (alerts on new or dropped promises, copilot tools).
    Bulk statements with RETURNING: row-by-row flushes cost ~85 ms each against Neon (~1 minute per report)."""
    from sqlalchemy import insert
    with session() as s:
        s.execute(delete(LedgerRow).where(LedgerRow.company_id == cid))
        s.execute(delete(Promise).where(Promise.company_id == cid))
        s.execute(delete(Actual).where(Actual.company_id == cid))
        aid = {}
        if actuals:
            vals = [{"company_id": cid, "document_id": a["doc_id"], "metric": a["metric"][:200], "period": a["period"][:16],
                     "value": a["value"], "unit": (a.get("unit") or "")[:40], "quote": a["quote"], "page": a["page"]}
                    for a in actuals]
            ids = s.execute(insert(Actual).returning(Actual.id, sort_by_parameter_order=True), vals).scalars().all()
            aid = {a["id"]: i for a, i in zip(actuals, ids)}
        if rows:
            vals = [{"company_id": cid, "document_id": r["promise"]["doc_id"], "said_in": (r["promise"].get("said_in") or "")[:16],
                     "speaker": (r["promise"].get("speaker") or "")[:160], "metric": r["promise"]["metric"][:200],
                     "target_low": r["promise"].get("target_low"), "target_high": r["promise"].get("target_high"),
                     "unit": (r["promise"].get("unit") or "")[:40], "horizon": (r["promise"].get("horizon") or "")[:40],
                     "quote": r["promise"]["quote"], "page": r["promise"]["page"]} for r in rows]
            pids = s.execute(insert(Promise).returning(Promise.id, sort_by_parameter_order=True), vals).scalars().all()
            s.execute(insert(LedgerRow), [{"company_id": cid, "promise_id": pid, "actual_id": aid.get((r["actual"] or {}).get("id")),
                                           "verdict": r["verdict"], "detail": {"tracking": r["tracking"], "promise_ref": r["promise"]["id"]}}
                                          for r, pid in zip(rows, pids)])


# ---------- public API ----------

_report_memo: dict[int, dict] = {}


def cached_report(symbol: str) -> dict | None:
    """Latest core report younger than REPORT_TTL (7 days). One small read (id + timestamp) on the fast read path; the report body
    comes from an in-process memo keyed by report id, so a cached report costs about one round trip."""
    from ..database import read_session_run

    def latest(s):
        return s.execute(select(Report.id, Report.created_at).join(Company, Company.id == Report.company_id)
                         .where(Company.nse_symbol == symbol.upper(), Report.kind == "core")
                         .order_by(Report.created_at.desc()).limit(1)).first()
    row = read_session_run(latest)
    if not row or datetime.now(timezone.utc) - row.created_at > REPORT_TTL:
        return None
    if row.id not in _report_memo:
        def body(s):
            rep = s.get(Report, row.id)
            return {"report_id": rep.id, **rep.data, "receipt": rep.receipt, "cached": True}
        _report_memo[row.id] = read_session_run(body)
        if len(_report_memo) > 50:
            _report_memo.pop(next(iter(_report_memo)))
    return present(_report_memo[row.id])


_running: dict[str, str] = {}
_lock = threading.Lock()


def start(symbol: str, force: bool = False) -> dict:
    """Returns {job_id, cached}. A cached report (< 7 days) returns at once; a running job for the symbol is reused."""
    symbol = symbol.upper().strip()
    if not force:
        hit = cached_report(symbol)
        if hit:
            return {"job_id": None, "cached": True, "report": hit}
    with _lock:
        if symbol in _running:
            return {"job_id": _running[symbol], "cached": False}
        job_id = str(uuid.uuid4())
        _running[symbol] = job_id
    with session() as s:
        s.add(ResearchJob(id=job_id, symbol=symbol, stage=STAGES[0], progress=0.0, status="running"))

    def work():
        try:
            run(symbol, job_id)
        except screener.NoSuchTicker as e:
            _job(job_id, status="failed", error=f"{symbol} isn't a listed company we can find ({e})")
        except Exception as e:  # noqa: BLE001
            log.exception("research failed for %s", symbol)
            _job(job_id, status="failed", error=str(e)[:500])
        finally:
            with _lock:
                _running.pop(symbol, None)
    threading.Thread(target=work, daemon=True).start()
    return {"job_id": job_id, "cached": False}


def plain(err: str | None) -> str:
    """A raw error as one plain sentence for the page; the raw text stays in the logs and llm_calls."""
    e = str(err or "")
    low = e.lower()
    if "403" in e or "forbidden" in low:
        return "the publisher's website refused the download"
    if "404" in e or "not found" in low:
        return "the document is no longer at its link"
    if "budget" in low or "cost cap" in low:
        return "the per-report cost cap was reached"
    if "per day" in low or "perday" in low or "quota" in low or "429" in e or "resource_exhausted" in low:
        return "today's free model quota is used up"
    if "504" in e or "deadline" in low or "timed out" in low or "timeout" in low:
        return "the model service took too long to answer"
    if "503" in e or "unavailable" in low or "high demand" in low:
        return "the model service was busy"
    if "scanned" in low or "no text layer" in low:
        return "the PDF is a scan with no text layer"
    return "an unexpected problem"


_NOTE_PREFIX = ("Synthesis unavailable:", "Counter-argument unavailable:")


def present(rep: dict) -> dict:
    """The report as the page shows it: raw errors in notes and sources become plain sentences, and a report that
    could not read most transcripts says so up front. Applied when serving, so stored reports benefit too."""
    if not rep:
        return rep
    out = dict(rep)
    notes = []
    for n in rep.get("notes") or []:
        pre = next((p for p in _NOTE_PREFIX if n.startswith(p)), None)
        notes.append(f"{pre} {plain(n)}; rebuild the report later to fill it in." if pre else n)
    srcs = []
    for s in rep.get("sources") or []:
        s = dict(s)
        if s.get("status") == "failed" and s.get("reason"):
            s["reason"] = plain(s["reason"])
        srcs.append(s)
    tr = [s for s in srcs if s.get("kind") == "transcript"]
    tr_read = sum(1 for s in tr if s.get("status") == "read")
    if tr and tr_read < len(tr):
        why = next((s["reason"] for s in tr if s.get("status") == "failed" and s.get("reason")), "they could not be read")
        msg = (f"{len(tr) - tr_read} of {len(tr)} earnings-call transcripts could not be read ({why}); "
               + ("this report uses the annual reports only." if tr_read == 0 else
                  f"the ledger uses the {tr_read} that could, and the annual reports."))
        if msg not in notes:
            notes.insert(0, msg)
    out["notes"], out["sources"] = notes, srcs
    return out


def job(job_id: str) -> dict | None:
    with session() as s:
        j = s.get(ResearchJob, job_id)
        if not j:
            return None
        out = {"id": j.id, "symbol": j.symbol, "stage": j.stage, "progress": j.progress, "status": j.status,
               "error": j.error, "stages": STAGES, "report_id": j.report_id}
        if j.report_id:
            rep = s.get(Report, j.report_id)
            out["report"] = present({"report_id": rep.id, **rep.data, "receipt": rep.receipt})
        if out["error"]:
            out["error"] = f"Research didn't finish: {plain(out['error'])}. Try again in a few minutes."
        return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    sym = sys.argv[1]
    force = "--force" in sys.argv
    if not force and (hit := cached_report(sym)):
        print(json.dumps(hit["receipt"], indent=1))
        sys.exit(0)
    jid = str(uuid.uuid4())
    with session() as s:
        s.add(ResearchJob(id=jid, symbol=sym.upper(), stage=STAGES[0], progress=0.0, status="running"))
    out = run(sym, jid)
    print(json.dumps({"credibility": out["credibility"], "sources": [(s["id"], s["kind"], s["period"], s["status"],
                                                                      s.get("kept"), s.get("dropped")) for s in out["sources"]],
                      "receipt": out["receipt"], "notes": out["notes"]}, indent=1, default=str))
