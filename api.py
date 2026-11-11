"""FINN API + static SPA host. One service: FastAPI + SQLite."""
import json, logging, os, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from finn import alerts, db, ingest, notify, pull, push

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("finn.api")
IST = ZoneInfo("Asia/Kolkata")
app = FastAPI(title="FINN")
from finn import web as web_v2  # noqa: E402  (v2 Postgres routes)
app.include_router(web_v2.router)
from finn import web_ui  # noqa: E402  (Phase 7 screen routes)
app.include_router(web_ui.router)
from finn import experience  # noqa: E402  (Phase 10: replay, journal, feedback)
app.include_router(experience.router)
app.middleware("http")(web_v2.timing_middleware)


@app.middleware("http")
async def _snapshot_fallback(request: Request, call_next):
    """DEMO_SNAPSHOT: if the database is unreachable (or DEMO_SNAPSHOT=1), main read routes answer from
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
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WINDOWS = {"24h": timedelta(hours=24), "3d": timedelta(days=3), "7d": timedelta(days=7), "30d": timedelta(days=30)}
TYPE_LABEL = {
    "results": "Results", "mgmt_change": "Mgmt change", "m_and_a": "M&A", "fundraise": "Fundraise",
    "order_win": "Order win", "credit_rating": "Credit rating", "dividend_action": "Dividend / action",
    "litigation_regulatory": "Legal / regulatory", "insider_pledge": "Insider / pledge",
    "board_meeting_notice": "Board meeting", "investor_meet": "Investor meet", "routine_compliance": "Compliance",
    "other": "Update",
}

# ---------------- pipeline ----------------
_refresh = {"running": False, "last": None, "error": None, "started_at": None}


def run_pipeline():
    out = {}
    for name, fn in (("ingest", ingest.run), ("push", push.run), ("alerts", alerts.evaluate), ("notify", notify.notify_new_alerts)):
        try:
            out[name] = fn()
        except Exception as e:  # one stage failing never stops the next
            log.exception("pipeline stage %s failed", name)
            out[name] = {"error": str(e)}
    return out


def _refresh_worker():
    _refresh.update(running=True, started_at=datetime.now(IST).isoformat(timespec="seconds"))
    try:
        _refresh["last"] = run_pipeline()
        _refresh["error"] = None
    except Exception as e:
        _refresh["error"] = str(e)
    finally:
        _refresh["running"] = False


@app.post("/api/refresh")
def refresh():
    if not _refresh["running"]:
        threading.Thread(target=_refresh_worker, daemon=True).start()
    return {"started": True}


@app.get("/api/refresh")
def refresh_status():
    return _refresh


# ---------------- feed ----------------

def _card(a: dict, names: dict) -> dict:
    return {
        "id": a["id"], "symbol": a["symbol"], "name": names.get(a["symbol"], a["symbol"]), "type": a["ann_type"],
        "type_label": TYPE_LABEL.get(a["ann_type"], "Update"), "score": a["score"], "bucket": a["bucket"],
        "filed_at": a["filed_at"], "subject": a["subject"], "category": a["category"], "subcategory": a["subcategory"],
        "one_liner": json.loads(a["one_liner"]) if a["one_liner"] else None,
        "enrichment": json.loads(a["enrichment_json"]) if a["enrichment_json"] else None,
        "pdf_url": a["pdf_url"], "status": a["status"], "error": a["error"], "reviewed": bool(a["reviewed"]),
        "source": "BSE",
    }


def _names():
    return {p["nse_symbol"]: p["name"] for p in db.portfolio()}


@app.get("/api/feed")
def feed(window: str = "24h", auto: int = 1):
    if window not in WINDOWS:
        raise HTTPException(400, "window must be one of 24h, 3d, 7d, 30d")
    now = datetime.now(IST)
    start = now - WINDOWS[window]
    note = None
    n_material = db.q("SELECT count(*) n FROM announcements WHERE processed=1 AND bucket<>'routine' AND filed_at>=?",
                      (start.isoformat(),), one=True)["n"]
    if n_material == 0 and auto:
        # Quiet window (weekends, holidays): step back to the most recent day(s) with material filings,
        # until at least 3 material items show, so the inbox is never empty.
        days = db.q("SELECT substr(filed_at,1,10) d, count(*) n FROM announcements WHERE processed=1 AND bucket<>'routine' "
                    "GROUP BY 1 ORDER BY 1 DESC LIMIT 10")
        total, day = 0, None
        for r in days:
            total += r["n"]
            day = r["d"]
            if total >= 3:
                break
        if day:
            start = datetime.fromisoformat(day).replace(tzinfo=IST)
            note = f"Quiet {window}. Showing material filings since {start.strftime('%d %b')}."
    rows = db.q("SELECT * FROM announcements WHERE processed=1 AND filed_at>=? ORDER BY score DESC, filed_at DESC", (start.isoformat(),))
    names = _names()
    cards = [_card(a, names) for a in rows]
    material_cards = [c for c in cards if c["bucket"] != "routine" and not c["reviewed"]]
    routine = [c for c in cards if c["bucket"] == "routine"]
    reviewed = [c for c in cards if c["bucket"] != "routine" and c["reviewed"]]
    return {
        "window": window, "since": start.isoformat(), "note": note,
        "counts": {"attention": sum(c["bucket"] == "attention" for c in material_cards),
                   "worth_a_look": sum(c["bucket"] == "worth_a_look" for c in material_cards),
                   "routine": len(routine), "reviewed": len(reviewed)},
        "cards": material_cards, "routine": routine, "reviewed": reviewed,
        "last_refresh": (db.q("SELECT max(created_at) t FROM ingest_log", one=True) or {}).get("t"),
    }


@app.get("/api/cards/{cid}")
def card(cid: int):
    a = db.q("SELECT * FROM announcements WHERE id=?", (cid,), one=True)
    if not a:
        raise HTTPException(404, "card not found")
    return _card(a, _names())


@app.post("/api/cards/{cid}/review")
def review(cid: int, undo: int = 0):
    db.x("UPDATE announcements SET reviewed=? WHERE id=?", (0 if undo else 1, cid))
    return {"ok": True}


# ---------------- alerts ----------------

@app.get("/api/alerts")
def list_alerts():
    rows = db.q("SELECT * FROM alerts ORDER BY date DESC, id DESC LIMIT 200")
    return {"alerts": rows, "unread": sum(1 for r in rows if not r["read"])}


@app.post("/api/alerts/{aid}/read")
def read_alert(aid: int):
    db.x("UPDATE alerts SET read=1 WHERE id=?", (aid,))
    return {"ok": True}


@app.post("/api/alerts/read_all")
def read_all():
    db.x("UPDATE alerts SET read=1")
    return {"ok": True}


class RuleIn(BaseModel):
    kind: str
    threshold: float | None = None
    types: str | None = None
    enabled: bool = True


@app.get("/api/rules")
def get_rules():
    return {"rules": list(alerts.rules().values()), "types": list(TYPE_LABEL), "type_labels": TYPE_LABEL}


@app.put("/api/rules")
def put_rules(rules: list[RuleIn]):
    for r in rules:
        db.x("UPDATE alert_rules SET threshold=?, types=?, enabled=? WHERE kind=?", (r.threshold, r.types, int(r.enabled), r.kind))
    return get_rules()


@app.post("/api/alerts/evaluate")
def evaluate_alerts():
    n = alerts.evaluate()
    return {"new": n, "notify": notify.notify_new_alerts()}


# ---------------- web push ----------------

@app.get("/api/push/key")
def push_key():
    return {"key": os.getenv("VAPID_PUBLIC_KEY", ""), "configured": notify.configured()}


@app.post("/api/push/subscribe")
async def subscribe(req: Request):
    sub = await req.json()
    if not sub.get("endpoint"):
        raise HTTPException(400, "subscription needs an endpoint")
    db.x("INSERT OR REPLACE INTO push_subscriptions(endpoint,json) VALUES(?,?)", (sub["endpoint"], json.dumps(sub)))
    return {"ok": True}


@app.post("/api/push/test")
def push_test():
    r = notify.send_to_all("FINN test", "Notifications are working. Tap to open Alerts.", "/#alerts")
    return r


# ---------------- portfolio + research ----------------

@app.get("/api/portfolio")
def portfolio():
    out = []
    for p in db.portfolio():
        px = db.q("SELECT date, close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 2", (p["nse_symbol"],))
        chg = ((px[0]["close"] - px[1]["close"]) / px[1]["close"] * 100) if len(px) == 2 else None
        out.append({**p, "close": px[0]["close"] if px else None, "change_pct": round(chg, 2) if chg is not None else None,
                    "as_of": px[0]["date"] if px else None})
    return {"portfolio": out}


@app.post("/research/{ticker}")
def research_start(ticker: str, force: int = 0):
    try:
        return {"job_id": pull.start(ticker, force=bool(force))}
    except pull.NoSuchTicker as e:
        raise HTTPException(400, str(e))


@app.get("/research/job/{job_id}")
def research_job(job_id: str):
    j = pull.job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.get("/api/research/recent")
def research_recent():
    return {"reports": db.q("SELECT symbol, created_at FROM research_reports ORDER BY created_at DESC LIMIT 10")}


STARTED_AT = datetime.utcnow().isoformat() + "Z"


@app.get("/health")
def liveness():
    """Keep-awake and platform health check. Deliberately touches no database, so a ping every 10 minutes
    never wakes Neon's compute or spends anything."""
    return {"ok": True, "service": "finn", "started_at": STARTED_AT}


@app.get("/api/health")
def health():
    return {"ok": True, "announcements": db.q("SELECT count(*) n FROM announcements", one=True)["n"]}


# ---------------- static ----------------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.json"), media_type="application/manifest+json")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.on_event("startup")
def _warm_feed():
    if os.getenv("FINN_NO_WARMER") != "1":
        from finn.feed import digest as _dg
        _dg.start_warmer()


@app.on_event("startup")
def _startup():
    db.portfolio()
    alerts.seed_rules()
    if os.getenv("FINN_SEED_ON_START") == "1" and not db.q("SELECT 1 FROM announcements LIMIT 1"):
        threading.Thread(target=_refresh_worker, daemon=True).start()
