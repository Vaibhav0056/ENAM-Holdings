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
