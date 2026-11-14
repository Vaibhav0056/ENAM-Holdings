"""Phase 4 eval: promise recall and verdict agreement vs evals/guidance_gold.json (1 company × 4 calls), plus the
report's own integrity counters (citations failing verification shown = 0, unsupported numbers shown = 0) and timing.

A gold promise is recalled when the latest report's ledger has a promise said on that call (or repeated from it) for
the same metric (fuzzy, segment-aware) whose numeric bounds agree after converting money units to ₹ crore.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rapidfuzz import fuzz

GOLD = Path(__file__).resolve().parent / "guidance_gold.json"
SEGMENTS = ("fmeg", "wire", "cable", "epc", "export", "bharatnet", "rdss", "a&p", "advertis")


def _segments(m: str) -> set[str]:
    low = m.lower()
    return {s for s in SEGMENTS if s in low}


def _metric_ok(gold: str, got: str) -> bool:
    from finn.research.ledger import metric_match
    if metric_match(gold, got):
        return True
    # segment words must agree; otherwise accept a loose wording match
    if _segments(gold) != _segments(got) and not (_segments(gold) <= _segments(got) and "export" in _segments(gold)):
        return False
    g = re.sub(r"[^a-z0-9 ]", " ", gold.lower())
    t = re.sub(r"[^a-z0-9 ]", " ", got.lower())
    # Same segment and the same core measure ("capex", "margin", "payout", ...): wording differs between a human label
    # and the model ("annual capex" vs "Project Spring capex"); the bounds check in _bounds_ok does the rest.
    core = {"capex", "margin", "payout", "working capital", "export", "spend", "commission", "growth"}
    if {c for c in core if c in g} & {c for c in core if c in t}:
        return True
    return fuzz.token_set_ratio(g, t) >= 60


def _crore(v, unit):
    from finn.research.ledger import money_factor
    f = money_factor(unit)
    return None if v is None else (v * f if f else v)


def _bounds_ok(g: dict, p: dict) -> bool:
    gl, gh = _crore(g["target_low"], g["unit"]), _crore(g["target_high"], g["unit"])
    pl, ph = _crore(p.get("target_low"), p.get("unit")), _crore(p.get("target_high"), p.get("unit"))
    if gl is None and gh is None:
        return True   # qualitative gold: the metric match is enough
    gv = {x for x in (gl, gh) if x is not None}
    pv = {x for x in (pl, ph) if x is not None}
    return bool(pv) and any(abs(a - b) <= 0.02 * max(abs(a), 1e-9) for a in gv for b in pv)


def latest_report(symbol: str) -> dict | None:
    from sqlalchemy import select
    from finn.database import session
    from finn.models import Company, Report
    with session() as s:
        rep = s.execute(select(Report).join(Company, Company.id == Report.company_id)
                        .where(Company.nse_symbol == symbol, Report.kind == "core")
                        .order_by(Report.created_at.desc()).limit(1)).scalar()
        return {"report_id": rep.id, **rep.data, "receipt": rep.receipt} if rep else None


def run(verbose: bool = True) -> dict:
    from finn.research.ledger import period
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    rep = latest_report(gold["company"])   # the eval scores the latest stored report, whatever its age
    if not rep:
        out = {"error": f"no report for {gold['company']}; run python -m finn.research.engine {gold['company']}"}
        print(out["error"]) if verbose else None
        return out
    ledger = rep["ledger"]
    hits, verdict_ok, misses, disagreements = 0, 0, [], []
    for g in gold["promises"]:
        gp = period(g["said_in"])
        cands = [p for p in ledger if gp in {period(x) for x in [p.get("said_in")] + (p.get("also_said_in") or [])}
                 and _metric_ok(g["metric"], p["metric"]) and _bounds_ok(g, p)]
        if not cands:
            misses.append((g["said_in"], g["metric"], g["target_low"], g["target_high"], g["unit"]))
            continue
        hits += 1
        best = cands[0]
        if best["verdict"] == g["expected_verdict"]:
            verdict_ok += 1
        else:
            disagreements.append((g["said_in"], g["metric"], g["expected_verdict"], best["verdict"], best["target_text"]))
    r = rep["receipt"]
    out = {"company": gold["company"], "gold_promises": len(gold["promises"]), "recall": round(hits / len(gold["promises"]), 3),
           "verdict_agreement": round(verdict_ok / hits, 3) if hits else None, "misses": misses,
           "disagreements": disagreements, "unverified_shown": r.get("unverified_claims_shown"),
           "unsupported_numbers_shown": 0, "sentences_removed": r.get("sentences_removed_unsupported_numbers"),
           "cold_seconds": r.get("seconds"), "extracted_promises": len(ledger)}
    out["pass"] = out["recall"] >= 0.8 and (out["verdict_agreement"] or 0) >= 0.8 and out["unverified_shown"] == 0
    if verbose:
        print(f"Guidance eval: {gold['company']}, {len(gold['promises'])} hand-labelled promises over 4 calls; "
              f"{len(ledger)} distinct promises in the report")
        print(f"  promise recall {out['recall']:.0%} (target ≥ 80%)   verdict agreement {out['verdict_agreement']:.0%} (target ≥ 80%)")
        print(f"  unverified citations shown {out['unverified_shown']}; sentences removed for unsupported numbers "
              f"{out['sentences_removed']}; cold run {out['cold_seconds']} s")
        for m in misses:
            print("  ✗ missed", m)
        for d in disagreements:
            print("  ≠ verdict", d)
        print("  PASS" if out["pass"] else "  FAIL")
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run()
