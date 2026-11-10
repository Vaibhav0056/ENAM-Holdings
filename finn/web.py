"""v2 HTTP routes (Postgres). Mounted into api.py; the Step 1-8 routes keep serving until the UI moves over."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request

from .feed import digest as dg

router = APIRouter()

# ---------- route latency (p50/p95 at /metrics/latency) ----------
_lat: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))


async def timing_middleware(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    _lat[f"{request.method} {path}"].append((time.perf_counter() - t0) * 1000)
    return response


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 1)


@router.get("/metrics/latency")
def latency():
    return {k: {"n": len(v), "p50_ms": _pct(list(v), 0.5), "p95_ms": _pct(list(v), 0.95)} for k, v in _lat.items() if v}


# ---------- digest ----------

@router.get("/digest")
def digest(window: str = "24h", start: str | None = None, end: str | None = None, as_of: str | None = None,
           peers: bool = False, request: Request = None):
    # `from`/`to` are reserved words in Python; accept them from the query string as the spec names them.
    qp = request.query_params if request else {}
    start = start or qp.get("from")
    end = end or qp.get("to")
    try:
        return dg.digest(window, start, end, as_of, include_peers=peers)
    except dg.WindowError as e:
        raise HTTPException(400, str(e))


@router.post("/api/state/checked")
def checked():
    return {"last_checked_at": dg.mark_checked()}


@router.post("/api/v2/cards/{filing_id}/review")
def review(filing_id: int, undo: bool = False):
    if not dg.mark_reviewed(filing_id, undo):
        raise HTTPException(404, "no such card")
    return {"ok": True, "reviewed": not undo}


# ---------- research (Pull) ----------

@router.post("/api/v2/research/{ticker}")
def research_start(ticker: str, force: bool = False):
    import re as _re
    from .research import engine
    t = ticker.upper().strip()
    if not _re.fullmatch(r"[A-Z0-9&\-]{1,20}", t):
        raise HTTPException(400, "not a valid NSE symbol")
    out = engine.start(t, force=force)
    if out.get("cached"):
        return {"cached": True, "report": out["report"]}
    return {"cached": False, "job_id": out["job_id"]}


@router.get("/api/v2/research/job/{job_id}")
def research_job(job_id: str):
    from .research import engine
    j = engine.job(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j


@router.get("/api/v2/research/job/{job_id}/events")
async def research_events(job_id: str):
    """Server-sent events: one `stage` event whenever the stage or progress changes, then `done` or `failed`."""
    import asyncio
    import json as _json
    from fastapi.responses import StreamingResponse
    from .research import engine

    async def gen():
        last = None
        for _ in range(1800):  # at most ~15 minutes
            j = await asyncio.to_thread(engine.job, job_id)
            if not j:
                yield "event: failed\ndata: {\"error\": \"no such job\"}\n\n"
                return
            cur = (j["stage"], j["progress"], j["status"])
            if cur != last:
                last = cur
                payload = {k: j[k] for k in ("stage", "progress", "status", "error", "stages", "report_id")}
                yield f"event: stage\ndata: {_json.dumps(payload)}\n\n"
            if j["status"] in ("done", "failed"):
                yield f"event: {j['status']}\ndata: {_json.dumps({'report_id': j['report_id'], 'error': j['error']})}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/api/v2/research/{ticker}")
def research_report(ticker: str):
    from .research import engine
    hit = engine.cached_report(ticker)
    if not hit:
        raise HTTPException(404, "no report in the last 24 hours; POST to start one")
    return hit


# ---------- alerts, rules, push, catalysts ----------

@router.get("/api/v2/alerts")
def alerts_list(limit: int = 100):
    from sqlalchemy import select as _select
    from .database import read_session_run
    from .fiscal import to_ist
    from .models import Alert, AlertRule, Company

    def q(s):
        rows = s.execute(_select(Alert, AlertRule.kind, Company.nse_symbol).join(AlertRule, AlertRule.id == Alert.rule_id)
                         .join(Company, Company.id == Alert.company_id)
                         .order_by(Alert.triggered_on.desc(), Alert.id.desc()).limit(limit)).all()
        return [{"id": a.id, "kind": kind, "symbol": sym, "date": a.triggered_on.isoformat(), "title": a.title,
                 "line": a.line, "link": a.deep_link, "filing_id": a.filing_id, "read": a.read,
                 "created_at": to_ist(a.created_at).isoformat()} for a, kind, sym in rows]
    items = read_session_run(q)
    return {"alerts": items, "unread": sum(1 for a in items if not a["read"])}


@router.post("/api/v2/alerts/{alert_id}/read")
def alert_read(alert_id: int):
    from .database import session as _s
    from .models import Alert
    with _s() as s:
        a = s.get(Alert, alert_id)
        if not a:
            raise HTTPException(404, "no such alert")
        a.read = True
    return {"ok": True}


@router.post("/api/v2/alerts/read_all")
def alerts_read_all():
    from sqlalchemy import update
    from .database import session as _s
    from .models import Alert
    with _s() as s:
        n = s.execute(update(Alert).where(Alert.read.is_(False)).values(read=True)).rowcount
    return {"ok": True, "marked": n}


@router.get("/api/v2/rules")
def rules_get():
    from sqlalchemy import select as _select
    from .database import session as _s
    from .models import AlertRule
    with _s() as s:
        return {"rules": [{"id": r.id, "kind": r.kind, "label": r.label, "params": r.params, "enabled": r.enabled}
                          for r in s.scalars(_select(AlertRule).order_by(AlertRule.id))]}


RULE_LIMITS = {"multiple": (1.2, 10.0), "pct": (1.0, 20.0), "min_new": (1, 20)}
FILING_TYPES = {"results", "mgmt_change", "governance", "m_and_a", "fundraise", "order_win", "credit_rating",
                "litigation_regulatory", "insider_pledge", "dividend_action"}


@router.put("/api/v2/rules/{kind}")
async def rules_put(kind: str, request: Request):
    from sqlalchemy import select as _select
    from .database import session as _s
    from .models import AlertRule
    body = await request.json()
    with _s() as s:
        r = s.scalar(_select(AlertRule).where(AlertRule.kind == kind))
        if not r:
            raise HTTPException(404, "no such rule")
        params = dict(r.params or {})
        for k, v in (body.get("params") or {}).items():
            if k in RULE_LIMITS:
                lo, hi = RULE_LIMITS[k]
                v = float(v)
                if not lo <= v <= hi:
                    raise HTTPException(400, f"{k} must be between {lo} and {hi}")
                params[k] = v
            elif k == "types":
                bad = set(v) - FILING_TYPES
                if bad:
                    raise HTTPException(400, f"unknown types {sorted(bad)}")
                params[k] = sorted(set(v))
        r.params = params
        if "enabled" in body:
            r.enabled = bool(body["enabled"])
        return {"kind": r.kind, "params": r.params, "enabled": r.enabled}


@router.post("/api/v2/alerts/evaluate")
def alerts_evaluate():
    from .alerting import engine as ae, notify
    out = ae.evaluate()
    out["push"] = notify.notify_new_alerts()
    return out


@router.get("/api/v2/push/key")
def push_key():
    from .alerting import notify
    return {"key": notify.public_key(), "configured": notify.configured()}


@router.post("/api/v2/push/subscribe")
async def push_subscribe(request: Request):
    from .alerting import notify
    try:
        sid = notify.subscribe(await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": sid}


@router.post("/api/v2/push/test")
def push_test():
    from .alerting import notify
    return notify.send_to_all("FINN test", "Notifications are working. Tap to open your Alerts.", "/#alerts")


@router.get("/api/v2/catalysts")
def catalysts(days: int = 7):
    from datetime import date as _date, timedelta as _td
    from sqlalchemy import select as _select
    from .database import read_session_run
    from .models import CalendarEvent, Company

    def q(s):
        today = _date.today()
        rows = s.execute(_select(CalendarEvent, Company.nse_symbol).join(Company, Company.id == CalendarEvent.company_id)
                         .where(CalendarEvent.event_date >= today, CalendarEvent.event_date <= today + _td(days=min(days, 30)),
                                Company.is_portfolio.is_(True))
                         .order_by(CalendarEvent.event_date)).all()
        return [{"date": e.event_date.isoformat(), "symbol": sym, "kind": e.kind, "title": e.title} for e, sym in rows]
    return {"days": days, "events": read_session_run(q)}
