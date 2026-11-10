"""python -m evals.run: every metric, one table. Each phase adds its eval here.

Labels marked claude-provisional were set by the build agent from the filing text, before tuning, and are meant to
be corrected by a human; the holdout set was labelled blind and never used for tuning.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import push_eval  # noqa: E402


def main() -> int:
    rows, ok = [], True
    for name, path in [("push (tuning set)", push_eval.LABELS),
                       ("push (blind holdout)", push_eval.LABELS.parent / "filings_holdout.csv")]:
        r = push_eval.run(verbose=False, labels=path)
        rows.append((name, "classification accuracy", f"{r['classification_accuracy']:.0%}", "≥ 90%",
                     r["classification_accuracy"] >= 0.9))
        rows.append((name, f"material recall in attention (n={r['material_n']})",
                     f"{r['material_recall_attention']:.0%}", "100%", r["material_recall_attention"] == 1.0))
        rows.append((name, "share of attention that is material", f"{r['attention_precision_proxy']:.0%}", "report", True))
    w = max(len(r[0]) + len(r[1]) for r in rows) + 3
    for suite, metric, value, target, passed in rows:
        print(f"{(suite + ' · ' + metric):<{w}} {value:>6}   target {target:<7} {'PASS' if passed else 'FAIL'}")
        ok &= passed
    print("ALL PASS" if ok else "SOME TARGETS MISSED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
