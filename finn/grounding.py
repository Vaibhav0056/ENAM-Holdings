"""Quote verification: every extracted fact must be findable on its cited page."""
import re
from rapidfuzz import fuzz

THRESHOLD = 85


def _norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_ok(quote: str, page_text: str, threshold: int = THRESHOLD) -> bool:
    q, p = _norm(quote), _norm(page_text)
    if len(q) < 4 or not p:
        return False
    if q in p:
        return True
    return fuzz.partial_ratio(q, p) >= threshold


def verify(items, pages: list[str], quote_attr="quote", page_attr="page"):
    """Keep items whose quote matches their cited page (1-indexed). Tolerates an off-by-one page."""
    kept = []
    for it in items:
        quote = it.get(quote_attr) if isinstance(it, dict) else getattr(it, quote_attr)
        page = it.get(page_attr) if isinstance(it, dict) else getattr(it, page_attr)
        try:
            page = int(page)
        except (TypeError, ValueError):
            continue
        for cand in (page, page - 1, page + 1):
            if 1 <= cand <= len(pages) and quote_ok(quote, pages[cand - 1]):
                if isinstance(it, dict):
                    it[page_attr] = cand
                else:
                    setattr(it, page_attr, cand)
                kept.append(it)
                break
    return kept
