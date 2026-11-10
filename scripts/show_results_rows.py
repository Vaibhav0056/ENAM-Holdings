"""Print the raw lines of a results statement that a human needs to fill evals/results_gold.json:
the unit/basis header and the Revenue / PBT / Tax / PAT rows, per page, straight from the PDF text.
It does not use the extractor, so gold values are read independently of it.

    python scripts/show_results_rows.py <filing_id>
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finn.enrich import docs  # noqa: E402

KEYS = re.compile(r"revenue from operations|interest earned|total income|profit before tax|tax expense|"
                  r"profit for the (period|quarter)|net profit|crore|lakh|million|consolidated|standalone|quarter ended", re.I)


def main(fid: int):
    doc_id, status = docs.filing_document(fid)
    print(f"filing {fid} document {doc_id} status {status}")
    path = docs.pdf_path(doc_id)
    pages = docs.plumber_pages(path) if path else docs.pages(doc_id)
    for n, t in enumerate(pages, 1):
        lines = [ln for ln in t.splitlines() if KEYS.search(ln)]
        if sum(1 for ln in lines if re.search(r"profit before tax", ln, re.I)) and len(lines) > 3:
            print(f"--- page {n}")
            for ln in lines[:40]:
                print("   ", ln[:200])


if __name__ == "__main__":
    main(int(sys.argv[1]))
