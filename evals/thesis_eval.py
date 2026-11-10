"""Phase 5 eval: does FINN's "affects the thesis?" (impact != none) agree with the label (evals/thesis_labeled.csv)?
Target: agreement >= 80 %."""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import select

LABELS = Path(__file__).resolve().parent / "thesis_labeled.csv"


def run(verbose: bool = True) -> dict:
    from finn.database import session
    from finn.models import Enrichment
    rows = list(csv.DictReader(LABELS.open(encoding="utf-8")))
    with session() as s:
        live = {e.filing_id: e for e in s.scalars(select(Enrichment).where(Enrichment.filing_id.in_([int(r["filing_id"]) for r in rows])))}
    agree, missing, diffs = 0, 0, []
    for r in rows:
        e = live.get(int(r["filing_id"]))
        if not e or e.thesis_impact is None:
            missing += 1
            diffs.append((r["filing_id"], r["symbol"], r["affects_thesis"], "not judged", r["headline"][:60]))
            continue
        pred = "yes" if e.thesis_impact != "none" else "no"
        if pred == r["affects_thesis"]:
            agree += 1
        else:
            diffs.append((r["filing_id"], r["symbol"], r["affects_thesis"], e.thesis_impact, r["headline"][:60]))
    out = {"n": len(rows), "agreement": round(agree / len(rows), 3), "not_judged": missing, "disagreements": diffs}
    out["pass"] = out["agreement"] >= 0.8
    if verbose:
        print(f"Thesis eval: {out['n']} cards, agreement {out['agreement']:.0%} (target ≥ 80%), not judged {missing}")
        for d in diffs:
            print("  ≠", d)
        print("  PASS" if out["pass"] else "  FAIL")
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run()
