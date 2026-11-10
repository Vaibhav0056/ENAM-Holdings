"""Phase 2 eval: classification accuracy and material recall in Needs attention.

Reads evals/filings_labeled.csv (true_type, material) and compares against the LIVE predictions in the database,
so it measures the current code, not the snapshot in the predicted_* columns.
Targets: classification >= 90 %, material recall in attention = 100 %.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from sqlalchemy import select

LABELS = Path(__file__).resolve().parent / "filings_labeled.csv"
TARGETS = {"classification_accuracy": 0.90, "material_recall_attention": 1.0}


def run(verbose: bool = True, labels: Path = LABELS) -> dict:
    from finn.database import session
    from finn.models import Enrichment
    rows = list(csv.DictReader(labels.open(encoding="utf-8")))
    ids = [int(r["filing_id"]) for r in rows]
    with session() as s:
        live = {e.filing_id: e for e in s.scalars(select(Enrichment).where(Enrichment.filing_id.in_(ids)))}
    correct, misses, confusion = 0, [], Counter()
    material = [r for r in rows if r["material"].strip().lower() == "yes"]
    recalled, missed_material, false_attention = 0, [], []
    for r in rows:
        e = live.get(int(r["filing_id"]))
        pred_t, pred_b = (e.type, e.bucket) if e else ("<missing>", "<missing>")
        if pred_t == r["true_type"]:
            correct += 1
        else:
            misses.append((r["filing_id"], r["symbol"], r["true_type"], pred_t, r["subject"][:70]))
            confusion[(r["true_type"], pred_t)] += 1
        if r["material"].strip().lower() == "yes":
            if pred_b == "attention":
                recalled += 1
            else:
                missed_material.append((r["filing_id"], r["symbol"], pred_b, r["subject"][:70]))
        elif pred_b == "attention":
            false_attention.append((r["filing_id"], r["symbol"], pred_t, r["subject"][:70]))
    out = {
        "n": len(rows), "label_source": Counter(r["label_source"] for r in rows).most_common(),
        "classification_accuracy": round(correct / len(rows), 3),
        "material_n": len(material), "material_recall_attention": round(recalled / len(material), 3) if material else None,
        "attention_precision_proxy": round(recalled / (recalled + len(false_attention)), 3) if (recalled + len(false_attention)) else None,
        "misclassified": misses, "missed_material": missed_material, "non_material_in_attention": false_attention,
    }
    out["pass"] = (out["classification_accuracy"] >= TARGETS["classification_accuracy"]
                   and (out["material_recall_attention"] or 0) >= TARGETS["material_recall_attention"])
    if verbose:
        print(f"Push eval on {out['n']} labelled filings ({out['label_source']})")
        print(f"  classification accuracy  {out['classification_accuracy']:.1%}   (target ≥ 90%)")
        print(f"  material recall in attention {out['material_recall_attention']:.0%} of {out['material_n']}   (target 100%)")
        print(f"  share of attention that is material {out['attention_precision_proxy']}")
        for m in misses:
            print("  ✗ type", m)
        for m in missed_material:
            print("  ✗ material not in attention", m)
        for m in false_attention:
            print("  · non-material in attention", m)
        print("  PASS" if out["pass"] else "  FAIL")
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run()
