"""BSE's structured results snapshot (probed in Phase 3): resultsSnapshot(scripcode) ->
{"currency_unit": "in Cr.", "periods": ["Sep-26", "Jun-26", "FY25-26"], "results_in_crores": {"fields": [...],
 "data": [["Revenue", "3,647.00", "2,974.00", "10,388.00"], ["Net Profit", ...], ...]}}.

It is BSE's own reading of the company's filing, so it is an independent check on PDF extraction. It has no
year-ago quarter, no expense lines and no stated basis, so it never replaces the PDF.
"""
from __future__ import annotations

import logging
import re
import tempfile
import time
from datetime import date, datetime, timezone

from ..database import session
from ..fiscal import quarter_of
from ..models import Company
from .common import log_run

log = logging.getLogger("finn.sources.bse_snapshot")
_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def period_label(p: str) -> str | None:
    """'Sep-26' -> 'Q2 FY27' (quarter ending Sep 2026); 'FY25-26' -> 'FY26'."""
    p = p.strip()
    m = re.fullmatch(r"FY(\d{2})-(\d{2})", p)
    if m:
        return f"FY{m.group(2)}"
    m = re.fullmatch(r"([A-Za-z]{3})-(\d{2})", p)
    if m and m.group(1).lower() in _MON:
        return quarter_of(date(2000 + int(m.group(2)), _MON[m.group(1).lower()], 1)).label()
    return None


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse(raw: dict) -> dict | None:
    block = raw.get("results_in_crores") or {}
    fields, data = block.get("fields") or [], block.get("data") or []
    if len(fields) < 2 or not data:
        return None
    periods = [period_label(p) for p in fields[1:]]
    out = {"unit": "₹ crore", "periods": [p for p in periods if p], "revenue": {}, "net_profit": {}, "opm_pct": {}}
    names = {"revenue": "revenue", "net profit": "net_profit", "opm %": "opm_pct"}
    for row in data:
        key = names.get(str(row[0]).strip().lower())
        if not key:
            continue
        for p, v in zip(periods, row[1:]):
            if p and _num(v) is not None:
                out[key][p] = _num(v)
    return out if out["revenue"] else None


def ttm_revenue(snap: dict | None) -> float | None:
    """Trailing revenue proxy for sizing orders: the last full fiscal year in the snapshot."""
    if not snap:
        return None
    years = [p for p in snap.get("revenue", {}) if not p.startswith("Q")]
    return snap["revenue"][max(years)] if years else None


def ingest(companies) -> dict:
    from bse import BSE
    out = {}
    with BSE(download_folder=tempfile.gettempdir()) as b:
        for c in companies:
            if not c.bse_code:
                continue
            try:
                snap = parse(b.resultsSnapshot(c.bse_code))
                with session() as s:
                    row = s.get(Company, c.id)
                    row.bse_snapshot, row.snapshot_at = snap, datetime.now(timezone.utc)
                out[c.nse_symbol] = {"periods": (snap or {}).get("periods")}
                log_run("bse_snapshot", c.id, 1 if snap else 0, 1, ok=snap is not None, error=None if snap else "empty")
            except Exception as e:  # noqa: BLE001
                out[c.nse_symbol] = {"error": str(e)[:200]}
                log_run("bse_snapshot", c.id, 0, 0, ok=False, error=str(e))
            time.sleep(0.5)
    return out
