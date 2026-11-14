"""FINN web app: JSON API, server-sent events and the static PWA.

Routes live in finn/web.py (feed, alerts, rules, push, research), finn/web_ui.py (holdings, cards, evidence, deep
dives, copilot) and finn/experience.py (replay, journal, feedback). This file wires them together with route timing,
the DEMO_SNAPSHOT fallback, health, static files and the startup jobs (feed warmer, refresh scheduler).

    python -m uvicorn api:app --port 8000      (or: python tasks.py serve)
"""
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from finn import experience, web, web_ui

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")

app = FastAPI(title="FINN")
app.include_router(web.router)
app.include_router(web_ui.router)
app.include_router(experience.router)
app.middleware("http")(web.timing_middleware)


@app.middleware("http")
async def _snapshot_fallback(request: Request, call_next):
    """DEMO_SNAPSHOT: if the database is unreachable (or DEMO_SNAPSHOT=1), the main read routes answer from
    data/demo_snapshot.json instead of failing; see finn/snapshot.py."""
    from finn import snapshot
    if request.method == "GET" and snapshot.forced():
        hit = snapshot.response(request)
        if hit:
            return hit
    try:
        return await call_next(request)
    except Exception as e:  # noqa: BLE001
        from sqlalchemy.exc import DBAPIError, OperationalError
        if request.method == "GET" and isinstance(e, (OperationalError, DBAPIError)):
            hit = snapshot.response(request)
            if hit:
                return hit
        raise


@app.get("/health")
def liveness():
    """Keep-awake and platform health check. Touches no database, so a ping every 10 minutes never wakes Neon's
    compute or spends anything."""
    return {"ok": True, "service": "finn", "started_at": STARTED_AT}


# ---------- refresh scheduler ----------

@app.get("/api/v2/scheduler")
def scheduler_status():
    from finn import scheduler
    return scheduler.state


@app.post("/api/v2/refresh")
def refresh_now():
    """Run a full refresh in the background (filings, prices, calendar, pipeline); poll /api/v2/scheduler."""
    import threading
    from finn import scheduler
    if scheduler.state["running"]:
        return {"started": False, "reason": "a refresh is already running"}
    threading.Thread(target=scheduler.run_once, args=(True,), daemon=True).start()
    return {"started": True}


@app.on_event("startup")
def _startup_jobs():
    if os.getenv("FINN_NO_WARMER") != "1":
        from finn.feed import digest
        digest.start_warmer()
    from finn import scheduler
    scheduler.start()


# ---------- static PWA ----------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.json"), media_type="application/manifest+json")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
