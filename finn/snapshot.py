"""DEMO_SNAPSHOT fallback (spec Phase 11).

`python scripts/export_snapshot.py` writes data/demo_snapshot.json: the feed windows, holdings and holding pages,
alerts, catalysts and the stored research reports, exactly as the API serves them. If the database is unreachable
(or DEMO_SNAPSHOT=1), the main read routes answer from that file, marked with an `X-FINN-Snapshot` header and a
`snapshot` field, so a demo never shows a blank app. Writes still need the database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

PATH = Path(__file__).resolve().parent.parent / "data" / "demo_snapshot.json"
_data: dict | None = None


def load() -> dict | None:
    global _data
    if _data is None and PATH.exists():
        _data = json.loads(PATH.read_text(encoding="utf-8"))
    return _data


def lookup(path: str, query: dict) -> dict | None:
    d = load()
    if not d:
        return None
    if path == "/digest":
        return d["digest"].get(query.get("window", "24h")) or d["digest"].get("7d")
    if path == "/api/v2/holdings":
        return d["holdings"]
    if path.startswith("/api/v2/holdings/"):
        return d["holding_pages"].get(path.rsplit("/", 1)[-1].upper())
    if path == "/api/v2/alerts":
        return d["alerts"]
    if path == "/api/v2/catalysts":
        return d["catalysts"]
    if path.startswith("/api/v2/research/") and path.count("/") == 4:
        return d["research"].get(path.rsplit("/", 1)[-1].upper())
    return None


def response(request: Request) -> JSONResponse | None:
    body = lookup(request.url.path, dict(request.query_params))
    if body is None:
        return None
    out = dict(body)
    out["snapshot"] = {"generated_at": load().get("generated_at"),
                       "note": "Live data is unavailable; showing the saved demo snapshot."}
    return JSONResponse(out, headers={"X-FINN-Snapshot": load().get("generated_at", "")})


def forced() -> bool:
    return os.getenv("DEMO_SNAPSHOT") == "1"
