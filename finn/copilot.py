"""Copilot (spec E2, Phase 9): a bounded tool-using agent over FINN's own data.

- At most MAX_STEPS tool steps chosen by the cheap tier; one strong-tier answer written from the gathered evidence only.
- Every tool result becomes evidence with an id (E1, E2, ...) carrying its source (document + page, filing, ledger).
- The answer is checked: every sentence must cite known evidence ids, numbers must appear in the evidence, advice
  language is removed. If nothing supports an answer, it says "Not in sources".
- Per-question cost cap; every call logged under the question's budget key.

    python -m finn.copilot "Why did Polycab push the EHV plant to FY28?" --symbol POLYCAB
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel
from sqlalchemy import select

from . import guards
from .database import session
from .llm import router
from .models import Company, Document, Enrichment, Filing, LLMCall, Page, Thesis

log = logging.getLogger("finn.copilot")
MAX_STEPS = 6
COST_CAP_USD = 0.02
NOT_IN_SOURCES = "Not in sources."


class Step(BaseModel):
    action: Literal["search_docs", "get_page", "get_metric", "get_filings", "get_thesis", "get_ledger", "answer"]
    query: Optional[str] = None
    symbol: Optional[str] = None
    document_id: Optional[int] = None
    page: Optional[int] = None
    metric: Optional[str] = None
    days: Optional[int] = None


class Sentence(BaseModel):
    text: str
    citations: list[str]


class Answer(BaseModel):
    sentences: list[Sentence]
    not_in_sources: bool


# ---------- tools (each returns a list of evidence items: {text, source}) ----------

def _cid(symbol: str | None) -> Company | None:
    if not symbol:
        return None
    with session() as s:
        c = s.scalar(select(Company).where(Company.nse_symbol == symbol.upper()))
        if c:
            s.expunge(c)
        return c


def t_search_docs(query: str | None, symbol: str | None, **_) -> list[dict]:
    from .retrieval import chunk_document, search
    c = _cid(symbol)
    if not c or not query:
        return [{"text": "search_docs needs a company symbol and a query.", "source": {"kind": "note"}}]
    with session() as s:
        ids = [d.id for d in s.scalars(select(Document).where(Document.company_id == c.id, Document.status == "parsed",
                                                              Document.kind.in_(["transcript", "annual_report"])))]
    from sqlalchemy import func
    from .models import Chunk
    for d in ids:   # chunks are cheap; embeddings may not exist yet (keyword leg still works)
        with session() as s:
            if not s.scalar(select(func.count(Chunk.id)).where(Chunk.document_id == d)):
                chunk_document(d)
    hits = search(query, ids, k=5)
    return [{"text": f"[{h['kind']} {h['period']}, page {h['page_from']}{', ' + h['speaker'] + ' (' + (h['role'] or '') + ')' if h['speaker'] else ''}] {h['text'][:900]}",
             "source": {"kind": h["kind"], "document_id": h["document_id"], "page": h["page_from"], "title": h["title"],
                        "period": h["period"], "url": h["url"]}} for h in hits] or \
        [{"text": f"No passages about '{query}' in {c.nse_symbol}'s stored documents.", "source": {"kind": "note"}}]


def t_get_page(document_id: int | None, page: int | None, **_) -> list[dict]:
    if not document_id or not page:
        return [{"text": "get_page needs document_id and page.", "source": {"kind": "note"}}]
    with session() as s:
        d, p = s.get(Document, document_id), s.get(Page, (document_id, page))
        if not d or not p:
            return [{"text": "That page isn't stored.", "source": {"kind": "note"}}]
        return [{"text": f"[{d.kind} {d.period}, page {page}] {p.text[:3000]}",
                 "source": {"kind": d.kind, "document_id": d.id, "page": page, "title": d.title, "url": d.url}}]


def t_get_metric(symbol: str | None, metric: str | None, **_) -> list[dict]:
    from .research.engine import cached_report
    from .research.ledger import metric_match
    c = _cid(symbol)
    if not c:
        return [{"text": "get_metric needs a company symbol.", "source": {"kind": "note"}}]
    out = []
    with session() as s:
        row = s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
                        .where(Filing.company_id == c.id, Enrichment.type == "results")
                        .order_by(Filing.filed_at.desc())).all()
        for f, e in row:
            p = e.payload or {}
            if p.get("ok"):
                t = "; ".join(f"{r['metric']} ₹{r['current']:,.0f} crore (QoQ {r['qoq']}%, YoY {r['yoy']}%)"
                              for r in p["table"] if r.get("found"))
                out.append({"text": f"[{c.nse_symbol} {p.get('period')} results, {p.get('basis')}] {t}",
                            "source": {"kind": "results_card", "filing_id": f.id, "url": f.pdf_url}})
                break
    rep = cached_report(c.nse_symbol)
    if rep and metric:
        for a in rep["actuals"]:
            if metric_match(metric, a["metric"]) or metric.lower() in a["metric"].lower():
                src = next((x for x in rep["sources"] if x["id"] == a["doc"]), {})
                out.append({"text": f"[{c.nse_symbol} {a['period']}, management on the call] {a['metric']}: {a['value']} {a['unit']}. Quote: \"{a['quote']}\"",
                            "source": {"kind": "transcript", "document_id": src.get("document_id"), "page": a["page"],
                                       "title": src.get("title"), "url": src.get("url")}})
                if len(out) >= 5:
                    break
    return out or [{"text": f"No verified figure for '{metric}' at {c.nse_symbol}.", "source": {"kind": "note"}}]


def t_get_filings(symbol: str | None, days: int | None, **_) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=min(days or 30, 90))
    with session() as s:
        q = (select(Filing, Enrichment, Company).join(Enrichment, Enrichment.filing_id == Filing.id)
             .join(Company, Company.id == Filing.company_id)
             .where(Filing.filed_at >= since, Enrichment.bucket.in_(["attention", "worth_a_look"]))
             .order_by(Enrichment.score.desc(), Filing.filed_at.desc()).limit(12))
        q = q.where(Company.nse_symbol == symbol.upper()) if symbol else q.where(Company.is_portfolio.is_(True))
        rows = s.execute(q).all()
    return [{"text": f"[{c.nse_symbol} filing {f.filed_at:%d %b %Y}, {e.type}, {e.bucket}] {e.headline}. {e.one_liner or ''} {e.why_it_matters or ''}",
             "source": {"kind": "filing", "filing_id": f.id, "url": f.pdf_url}} for f, e, c in rows] or \
        [{"text": "No material filings in that window.", "source": {"kind": "note"}}]


def t_get_thesis(symbol: str | None, **_) -> list[dict]:
    c = _cid(symbol)
    if not c:
        return [{"text": "get_thesis needs a company symbol.", "source": {"kind": "note"}}]
    from .models import ThesisStatus
    with session() as s:
        t = s.get(Thesis, c.id)
        st = s.scalar(select(ThesisStatus).where(ThesisStatus.company_id == c.id).order_by(ThesisStatus.at.desc()).limit(1))
        if not t:
            return [{"text": f"No thesis for {c.nse_symbol}.", "source": {"kind": "note"}}]
        k = "; ".join(f"{x['metric']} {x['op']} {x['threshold']}%" for x in t.kpis or []) or "none set"
        b = "; ".join(x["text"] for x in t.breakers or [])
        return [{"text": f"[{c.nse_symbol} thesis, {t.source}] {t.statement} KPIs: {k}. Breakers: {b}. "
                         f"Status: {st.status if st else 'intact'} ({st.reason if st else 'no events'}).",
                 "source": {"kind": "thesis", "symbol": c.nse_symbol}}]


def t_get_ledger(symbol: str | None, **_) -> list[dict]:
    from .research.engine import cached_report
    c = _cid(symbol)
    rep = cached_report(c.nse_symbol) if c else None
    if not rep:
        return [{"text": f"No promise ledger for {symbol} yet (open its research first).", "source": {"kind": "note"}}]
    cr = rep["credibility"]
    head = (f"[{c.nse_symbol} promise ledger] credibility {cr['score'] if cr['score'] is not None else 'not scored'}"
            f" on {cr['measured']} matured promises of {cr['total']}; {cr['vagueness_pct']}% too vague to check; "
            f"counts {cr['counts']}.")
    items = [{"text": head, "source": {"kind": "ledger", "report_id": rep["report_id"]}}]
    for l in [x for x in rep["ledger"] if x["verdict"] in ("delivered", "partial", "missed")][:4] + \
             [x for x in rep["ledger"] if x["verdict"] == "pending"][:3]:
        src = next((x for x in rep["sources"] if x["id"] == l["doc"]), {})
        items.append({"text": f"[{c.nse_symbol} promise said {l['said_in']}] {l['metric']} {l['target_text']} by {l['horizon']}: "
                              f"{l['verdict']}{' (' + l['tracking'] + ')' if l.get('tracking') else ''}. Quote: \"{l['quote']}\"",
                      "source": {"kind": "transcript", "document_id": src.get("document_id"), "page": l["page"],
                                 "title": src.get("title"), "url": src.get("url")}})
    return items


TOOLS: dict[str, Callable[..., list[dict]]] = {"search_docs": t_search_docs, "get_page": t_get_page, "get_metric": t_get_metric,
                                               "get_filings": t_get_filings, "get_thesis": t_get_thesis, "get_ledger": t_get_ledger}


# ---------- the loop ----------

def ask(question: str, symbol: str | None = None, emit: Callable[[str, dict], None] | None = None) -> dict:
    emit = emit or (lambda kind, data: None)
    t0 = time.monotonic()
    qid = str(uuid.uuid4())
    budget = f"copilot:{qid}"
    scope = f"company {symbol.upper()}" if symbol else "the whole portfolio"
    evidence: list[dict] = []
    calls_made: list[str] = []
    for step_no in range(1, MAX_STEPS + 1):
        obs = "\n".join(f"E{i}: {e['text'][:700]}" for i, e in enumerate(evidence, 1)) or "(none yet)"
        try:
            st = router.call("copilot_step", "copilot_step", {"scope": scope, "question": question, "observations": obs,
                                                               "steps_used": step_no - 1, "max_steps": MAX_STEPS},
                             Step, budget_key=budget).data
        except Exception as e:  # noqa: BLE001
            log.warning("copilot step failed: %s", e)
            break
        if st.action == "answer":
            break
        args = st.model_dump(exclude={"action"})
        if not args.get("symbol") and symbol and st.action != "get_filings":
            args["symbol"] = symbol
        if st.action == "search_docs" and not args.get("query"):
            args["query"] = question   # the step model often omits the query; the question itself is a good one
        sig = json.dumps([st.action, args], sort_keys=True)
        if sig in calls_made:
            break
        calls_made.append(sig)
        emit("step", {"n": step_no, "tool": st.action, "args": {k: v for k, v in args.items() if v is not None}})
        try:
            got = TOOLS[st.action](**args)
        except Exception as e:  # noqa: BLE001
            got = [{"text": f"{st.action} failed: {str(e)[:120]}", "source": {"kind": "note"}}]
        evidence += got[:6]
        if spent(budget) >= COST_CAP_USD:
            emit("note", {"text": "Cost cap reached; answering from what was found."})
            break
    # Fallback plan: if the loop ended without real evidence (a failed or repeated step), run the fixed plan that
    # answers most questions: search the documents, the recent filings and the promise ledger.
    if not any(e["source"].get("kind") != "note" for e in evidence):
        plan = [("search_docs", {"query": question, "symbol": symbol}), ("get_filings", {"symbol": symbol, "days": 90}),
                ("get_ledger", {"symbol": symbol})] if symbol else [("get_filings", {"symbol": None, "days": 30})]
        for tool, args in plan[:max(1, MAX_STEPS - len(calls_made))]:
            emit("step", {"n": len(calls_made) + 1, "tool": tool, "args": {k: v for k, v in args.items() if v is not None},
                          "fallback": True})
            calls_made.append(json.dumps([tool, args], sort_keys=True))
            try:
                evidence += TOOLS[tool](**args)[:6]
            except Exception as e:  # noqa: BLE001
                log.warning("fallback %s failed: %s", tool, e)
    ev_text = "\n".join(f"E{i}: {e['text']}" for i, e in enumerate(evidence, 1)) or "(no evidence)"
    real = [i for i, e in enumerate(evidence, 1) if e["source"].get("kind") != "note"]
    result = {"question": question, "scope": scope, "steps": [json.loads(c) for c in calls_made]}
    if not real:
        result.update(answer=[{"text": NOT_IN_SOURCES + " FINN found nothing in its stored filings, transcripts or reports "
                                        "that answers this.", "citations": []}], not_in_sources=True, evidence=[])
    else:
        try:
            a = router.call("copilot_answer", "copilot_answer", {"scope": scope, "question": question, "evidence": ev_text},
                            Answer, budget_key=budget).data
            sentences, removed = verify_answer(a, evidence)
            result.update(answer=sentences or [{"text": NOT_IN_SOURCES, "citations": []}],
                          not_in_sources=a.not_in_sources or not sentences, removed_sentences=removed)
        except Exception as e:  # noqa: BLE001
            result.update(answer=[{"text": f"Couldn't write an answer just now ({str(e)[:80]}).", "citations": []}],
                          not_in_sources=True, removed_sentences=0)
        cited = sorted({c for s in result["answer"] for c in s["citations"]}, key=lambda x: int(x[1:]))
        result["evidence"] = [{"id": c, "text": evidence[int(c[1:]) - 1]["text"][:600], **evidence[int(c[1:]) - 1]["source"]}
                              for c in cited]
    with session() as s:
        cs = s.scalars(select(LLMCall).where(LLMCall.budget_key == budget)).all()
    result["receipt"] = {"seconds": round(time.monotonic() - t0, 1), "tool_steps": len(calls_made),
                         "llm_calls": sum(1 for c in cs if c.ok and not c.cache_hit), "cost_usd": round(sum(c.cost_usd for c in cs), 4),
                         "models": sorted({c.model for c in cs if c.ok})}
    emit("answer", result)
    return result


def spent(budget: str) -> float:
    return router.spent(budget)


def verify_answer(a: Answer, evidence: list[dict]) -> tuple[list[dict], int]:
    """Keep sentences that cite real evidence and pass the number and advice guards. A not-in-sources answer may
    stand without citations."""
    allowed = guards.allowed_numbers("\n".join(e["text"] for e in evidence))
    ok_ids = {f"E{i}" for i in range(1, len(evidence) + 1)}
    out, removed = [], 0
    for s in a.sentences:
        cites = [c for c in s.citations if c in ok_ids]
        raw = re.sub(r"\s*[\[(]\s*(?:E\d+\s*,?\s*)+[\])]", "", s.text).strip()
        text, n, adv = guards.scrub(raw, allowed)
        if adv:
            removed += 1
            continue
        if not text or (not cites and not a.not_in_sources):
            removed += 1
            continue
        out.append({"text": text, "citations": cites})
    return out, removed


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    q = sys.argv[1]
    sym = sys.argv[sys.argv.index("--symbol") + 1] if "--symbol" in sys.argv else None
    r = ask(q, sym, emit=lambda k, d: print(k, json.dumps(d, default=str)[:200]) if k != "answer" else None)
    print(json.dumps({k: r[k] for k in ("answer", "not_in_sources", "evidence", "receipt", "steps")}, indent=1, default=str)[:3000])
