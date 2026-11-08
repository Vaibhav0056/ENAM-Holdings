"""FINN API + static SPA host."""
import logging, threading
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from finn import db, ingest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = FastAPI(title="FINN")
_refresh = {"running": False, "last": None, "error": None}


def _pipeline():
    _refresh["running"] = True
    try:
        _refresh["last"] = {"ingest": ingest.run()}
        _refresh["error"] = None
    except Exception as e:
        logging.exception("refresh failed")
        _refresh["error"] = str(e)
    finally:
        _refresh["running"] = False


@app.post("/api/refresh")
def refresh():
    if not _refresh["running"]:
        threading.Thread(target=_pipeline, daemon=True).start()
    return {"started": True}


@app.get("/api/refresh")
def refresh_status():
    return _refresh


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
