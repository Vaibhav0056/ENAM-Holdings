"""Phase 3 eval: results extraction vs hand-read gold values (evals/results_gold.json).

Each gold entry lists, for revenue / PBT / PAT, the current, previous-quarter and year-ago figures exactly as printed
on the statement, with the printed unit and basis. A cell matches when the extracted ₹ crore value is within 0.5 %
(or ₹0.1 crore) of the gold value converted to ₹ crore. A unit error is a cell off by a factor of 10/100/1000/1e5.
Targets: exact match >= 90 % of cells; unit errors = 0. Entries with gold "status": "unparseable" check that the card
says so instead of showing numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

GOLD = Path(__file__).resolve().parent / "results_gold.json"
COLS = {"current": "current", "previous": "previous", "year_ago": "year_ago"}


def run(verbose: bool = True) -> dict:
    from finn import fiscal
    from finn.database import session
    from finn.models import Enrichment
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    entries = [g for g in gold["filings"] if g.get("values") or g.get("status")]
    with session() as s:
        live = {e.filing_id: e.payload or {} for e in
                s.scalars(select(Enrichment).where(Enrichment.filing_id.in_([g["filing_id"] for g in entries])))}
    cells = matched = unit_errors = 0
    honest_abstentions = abstain_total = 0
    misses, basis_mismatch = [], []
    for g in entries:
        p = live.get(g["filing_id"], {})
        if g.get("status") == "unparseable":
            abstain_total += 1
            honest_abstentions += int(not p.get("ok"))
            continue
        if p.get("basis") and g.get("basis") and p["basis"] != g["basis"]:
            basis_mismatch.append((g["symbol"], g["basis"], p["basis"]))
        table = {r["metric"]: r for r in p.get("table", []) if r.get("found")}
        for metric, vals in g["values"].items():
            for col, gv in vals.items():
                if gv is None:
                    continue
                cells += 1
                want = fiscal.to_crore(gv, g["unit"])
                got = table.get(metric, {}).get(COLS[col])
                if got is not None and abs(got - want) <= max(0.005 * abs(want), 0.1):
                    matched += 1
                    continue
                if got is not None and want:
                    ratio = abs(got / want) if want else 0
                    if any(abs(ratio - f) / f < 0.01 for f in (10, 100, 1000, 1e5, 0.1, 0.01, 0.001, 1e-5)):
                        unit_errors += 1
                misses.append((g["symbol"], metric, col, want, got))
    out = {"filings": len(entries), "cells": cells, "exact_match": round(matched / cells, 3) if cells else None,
           "unit_errors": unit_errors, "basis_mismatch": basis_mismatch, "misses": misses,
           "scanned_pdfs_abstained": f"{honest_abstentions}/{abstain_total}"}
    out["pass"] = bool(cells) and out["exact_match"] >= 0.9 and unit_errors == 0
    if verbose:
        print(f"Results eval: {out['filings']} filings, {cells} cells")
        print(f"  exact match {out['exact_match']:.1%} (target ≥ 90%)   unit errors {unit_errors} (target 0)")
        print(f"  scanned PDFs that honestly show no numbers: {out['scanned_pdfs_abstained']}")
        for b in basis_mismatch:
            print("  basis differs from gold:", b)
        for m in misses:
            print("  ✗", m)
        print("  PASS" if out["pass"] else "  FAIL")
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run()
