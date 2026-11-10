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
