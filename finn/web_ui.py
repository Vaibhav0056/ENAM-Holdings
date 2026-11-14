"""Routes the Phase 7 screens need: holdings with thesis status, a holding page, the thesis editor, card detail,
evidence pages and user state. Reads use the fast read path."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from .database import read_session_run, session
from .feed.digest import _card
from .fiscal import to_ist
from .models import (CalendarEvent, Company, Document, Enrichment, Filing, Page, Price, Thesis, ThesisStatus,
                     UserState)

router = APIRouter()


def _latest_status(s) -> dict[int, ThesisStatus]:
    out = {}
    for st in s.scalars(select(ThesisStatus).order_by(ThesisStatus.at)):
        out[st.company_id] = st
    return out


@router.get("/api/v2/holdings")
def holdings():
    def q(s):
        cos = list(s.scalars(select(Company).where(Company.is_portfolio.is_(True)).order_by(Company.nse_symbol)))
        status = _latest_status(s)
        since = datetime.now(timezone.utc) - timedelta(days=7)
        att = dict(s.execute(select(Filing.company_id, func.count()).join(Enrichment, Enrichment.filing_id == Filing.id)
                             .where(Filing.filed_at >= since, Enrichment.bucket == "attention",
                                    Enrichment.reviewed_at.is_(None)).group_by(Filing.company_id)).all())
        last = defaultdict(list)
        for cid, c in s.execute(select(Price.company_id, Price.adj_close)
                                .where(Price.date >= datetime.now().date() - timedelta(days=10)).order_by(Price.date)):
            if cid:
                last[cid].append(c)
        out = []
        for c in cos:
            st, px = status.get(c.id), last.get(c.id, [])
            out.append({"symbol": c.nse_symbol, "name": c.name, "sector": c.industry or c.sector,
                        "status": st.status if st else "intact", "reason": st.reason if st else None,
                        "close": px[-1] if px else None,
                        "day_pct": round((px[-1] / px[-2] - 1) * 100, 2) if len(px) >= 2 and px[-2] else None,
                        "unreviewed_attention_7d": att.get(c.id, 0)})
        order = {"at_risk": 0, "watch": 1, "intact": 2}
        out.sort(key=lambda h: (order.get(h["status"], 3), -h["unreviewed_attention_7d"], h["symbol"]))
        return out
    return {"holdings": read_session_run(q)}


@router.get("/api/v2/holdings/{symbol}")
def holding(symbol: str):
    from .research.engine import cached_report

    def q(s):
        c = s.scalar(select(Company).where(Company.nse_symbol == symbol.upper()))
        if not c:
            return None
        t = s.get(Thesis, c.id)
        timeline = [{"status": x.status, "reason": x.reason, "at": to_ist(x.at).isoformat(), "filing_id": x.filing_id}
                    for x in s.scalars(select(ThesisStatus).where(ThesisStatus.company_id == c.id)
                                       .order_by(ThesisStatus.at.desc()))]
        since = datetime.now(timezone.utc) - timedelta(days=90)
        rows = s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
                         .where(Filing.company_id == c.id, Filing.filed_at >= since, Enrichment.bucket != "routine")
                         .order_by(Filing.filed_at.desc()).limit(25)).all()
        ev = [{"date": e.event_date.isoformat(), "kind": e.kind, "title": e.title} for e in
              s.scalars(select(CalendarEvent).where(CalendarEvent.company_id == c.id,
                                                    CalendarEvent.event_date >= datetime.now().date())
                        .order_by(CalendarEvent.event_date).limit(5))]
        prices = [{"date": d.isoformat(), "close": round(px, 2)} for d, px in
                  s.execute(select(Price.date, Price.adj_close).where(Price.company_id == c.id, Price.adj_close.is_not(None),
                                                                      Price.date >= datetime.now().date() - timedelta(days=95))
                            .order_by(Price.date))]
        return {"prices": prices, "symbol": c.nse_symbol, "name": c.name, "sector": c.industry or c.sector, "is_portfolio": c.is_portfolio,
                "thesis": {"statement": t.statement, "kpis": t.kpis, "breakers": t.breakers, "source": t.source,
                           "citations": t.citations, "updated_at": to_ist(t.updated_at).isoformat()} if t else None,
                "timeline": timeline, "cards": [_card(f, e, c) for f, e in rows], "catalysts": ev}
    h = read_session_run(q)
    if not h:
        raise HTTPException(404, "not a company FINN tracks")
    # KPI check against the latest verified results card: the same numbers the status engine uses, computed here
    from .thesis.kpi import check, kpi_values
    latest = next((c for c in h["cards"] if c["type"] == "results" and (c.get("payload") or {}).get("ok")), None)
    h["kpi_check"] = None if not (latest and h["thesis"]) else {
        "filing_id": latest["id"], "period": latest["payload"].get("period"),
        "rows": check(h["thesis"]["kpis"] or [], kpi_values(latest["payload"]))}
    rep = cached_report(symbol)
    h["research"] = None if not rep else {
        "report_id": rep["report_id"], "credibility": rep["credibility"],
        "generated_at": rep["receipt"]["generated_at"], "sources_read": rep["receipt"]["sources_read"]}
    return h


@router.put("/api/v2/holdings/{symbol}/thesis")
async def thesis_put(symbol: str, request: Request):
    from .thesis import guardian
    from .thesis.kpi import KPI_METRICS
    body = await request.json()
    statement = (body.get("statement") or "").strip()
    if not 10 <= len(statement) <= 1200:
        raise HTTPException(400, "the thesis statement should be 10-1200 characters")
    kpis = []
    for k in body.get("kpis") or []:
        if k.get("metric") not in KPI_METRICS or k.get("op") not in (">=", "<=") or \
                not isinstance(k.get("threshold"), (int, float)):
            raise HTTPException(400, f"bad KPI {k}")
        kpis.append({"metric": k["metric"], "op": k["op"], "threshold": float(k["threshold"]),
                     "why": (k.get("why") or "")[:200], "evidence": []})
    breakers = [{"text": b.strip()[:300], "evidence": []} for b in (body.get("breakers") or [])
                if isinstance(b, str) and b.strip()]
    with session() as s:
        c = s.scalar(select(Company).where(Company.nse_symbol == symbol.upper()))
        if not c:
            raise HTTPException(404, "not a company FINN tracks")
        t = s.get(Thesis, c.id) or Thesis(company_id=c.id)
        t.statement, t.kpis, t.breakers, t.source = statement, kpis, breakers, "user"
        s.merge(t)
    return {"ok": True, "status": guardian.statuses().get(symbol.upper())}


@router.get("/api/v2/cards/{filing_id}")
def card(filing_id: int):
    def q(s):
        f = s.get(Filing, filing_id)
        if not f:
            return None
        e, c = s.get(Enrichment, filing_id), s.get(Company, f.company_id)
        out = _card(f, e, c)
        doc = s.scalar(select(Document).where(Document.company_id == f.company_id, Document.url == f.pdf_url)) \
            if f.pdf_url else None
        out.update(document_id=doc.id if doc and doc.status == "parsed" else None, body=f.body, subject=f.subject)
        return out
    c = read_session_run(q)
    if not c:
        raise HTTPException(404, "no such card")
    return c


@router.get("/api/v2/documents/{doc_id}/pages/{page_no}")
def document_page(doc_id: int, page_no: int):
    def q(s):
        d, p = s.get(Document, doc_id), s.get(Page, (doc_id, page_no))
        if not d or not p:
            return None
        return {"document_id": d.id, "title": d.title, "kind": d.kind, "period": d.period, "url": d.url,
                "page": page_no, "pages": d.pages, "text": p.text}
    r = read_session_run(q)
    if not r:
        raise HTTPException(404, "page not found")
    return r


@router.get("/api/v2/deep/{symbol}/{recipe}")
def deep_dive(symbol: str, recipe: str, force: bool = False):
    """A deep dive: cached (< 24 h) results return at once; otherwise the recipe runs (a few seconds to a minute)."""
    from .deep import engine as deep
    if recipe not in deep.RUNNERS:
        raise HTTPException(404, f"unknown deep dive; try one of {', '.join(deep.RECIPES)}")
    try:
        return deep.run(symbol, recipe, force=force)
    except deep.NeedsResearch as e:
        raise HTTPException(409, str(e))


@router.get("/api/v2/deep")
def deep_list():
    from .deep import engine as deep
    return {"recipes": [{"id": r, "title": deep.TITLES[r]} for r in deep.RECIPES]}


@router.get("/api/v2/copilot")
async def copilot_stream(q: str, symbol: str | None = None):
    """Server-sent events: `step` for each tool call, `note`, then one `answer` with cited sentences."""
    import asyncio
    import json as _json
    import threading
    from fastapi.responses import StreamingResponse
    from . import copilot

    q = (q or "").strip()
    if not 3 <= len(q) <= 400:
        raise HTTPException(400, "ask a question of 3-400 characters")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(kind, data):
        loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

    def work():
        try:
            copilot.ask(q, symbol.upper() if symbol else None, emit=emit)
        except Exception as e:  # noqa: BLE001
            emit("error", {"error": str(e)[:200]})
        finally:
            emit("end", {})
    threading.Thread(target=work, daemon=True).start()

    async def gen():
        while True:
            kind, data = await queue.get()
            if kind == "end":
                return
            yield f"event: {kind}\ndata: {_json.dumps(data, default=str)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/api/v2/state")
def state():
    def q(s):
        st = s.get(UserState, 1)
        filing = s.scalar(select(func.max(Filing.filed_at)))
        price = s.scalar(select(func.max(Price.date)).where(Price.company_id.is_not(None)))
        return {"last_checked_at": to_ist(st.last_checked_at).isoformat() if st and st.last_checked_at else None,
                "latest_filing_at": to_ist(filing).isoformat() if filing else None,
                "latest_price_date": price.isoformat() if price else None}
    out = read_session_run(q)
    try:
        from .ingest import freshness
        out["freshness"] = freshness()
    except Exception:  # noqa: BLE001 - freshness is a nicety; never fail the state call over it
        out["freshness"] = None
    return out
