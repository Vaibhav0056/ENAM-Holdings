"""Idempotent seed: companies from data/companies.csv (validated in Phase 0), default alert rules, user state."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .database import session
from .models import AlertRule, Company, UserState

COMPANIES_CSV = Path(__file__).resolve().parent.parent / "data" / "companies.csv"

DEFAULT_RULES = [
    ("volume_spike", "Volume ≥ N × 20-day median", {"multiple": 2.0}),
    ("price_move", "Daily move ≥ ±X %", {"pct": 5.0}),
    ("announcement_type", "Material filing types", {"types": ["results", "mgmt_change", "governance", "m_and_a"]}),
    ("new_promise", "New or dropped management promises", {"min_new": 1}),
    ("peer_read_across", "Material filing by a research peer", {"types": ["results", "m_and_a", "governance"]}),
]


def seed_companies(path: Path = COMPANIES_CSV) -> dict:
    rows = [r for r in csv.DictReader(path.open(encoding="utf-8")) if r["status"] == "ok"]
    added = updated = 0
    now = datetime.now(timezone.utc)
    with session() as s:
        by_sym = {c.nse_symbol: c for c in s.scalars(select(Company))}
        for r in rows:
            c = by_sym.get(r["nse_symbol"])
            if c is None:
                c = Company(nse_symbol=r["nse_symbol"], name=r["name"] or r["nse_symbol"])
                s.add(c)
                by_sym[c.nse_symbol] = c
                added += 1
            else:
                updated += 1
            c.bse_code = r["bse_code"] or None
            c.yahoo_symbol = r["yahoo_symbol"]
            c.isin = r["isin"] or None
            c.sector = r["sector"] or None
            c.industry = r["industry"] or None
            c.is_portfolio = r["is_portfolio"] == "True"
            c.note = r["note"] or None
            c.validated_at = now
            if not c.name or c.name == c.nse_symbol:
                c.name = r["name"] or r["nse_symbol"]
        s.flush()
        for r in rows:  # second pass: peer links need ids
            if r["peer_of"]:
                by_sym[r["nse_symbol"]].peer_of = by_sym[r["peer_of"]].id
    return {"companies": len(rows), "added": added, "updated": updated}


def seed_rules() -> int:
    n = 0
    with session() as s:
        have = set(s.scalars(select(AlertRule.kind)))
        for kind, label, params in DEFAULT_RULES:
            if kind not in have:
                s.add(AlertRule(kind=kind, label=label, params=params, enabled=True))
                n += 1
        if not s.get(UserState, 1):
            s.add(UserState(id=1, prefs={}))
    return n


def run() -> dict:
    out = seed_companies()
    out["rules_added"] = seed_rules()
    return out


if __name__ == "__main__":
    print(run())
