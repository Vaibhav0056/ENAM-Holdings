"""Quote verification: every extracted fact must be findable on its cited page."""
import re
from rapidfuzz import fuzz

THRESHOLD = 90  # spec §7: rapidfuzz partial_ratio >= 90 on the cited page (±1)


def _norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(s: str) -> set[str]:
    """Every figure in the text, normalised ('INR1,600' -> '1600')."""
    out = set()
    for n in _NUM_RE.findall(s or ""):
        try:
            out.add("%g" % float(n.replace(",", "")))
        except ValueError:
            continue
    return out


def value_in_quote(value, quote: str) -> bool:
    """An extracted figure must be printed in its own quote, not computed by the model (a midpoint, a sum)."""
    return value is None or "%g" % abs(value) in numbers(quote)


def quote_ok(quote: str, page_text: str, threshold: int = THRESHOLD) -> bool:
    q, p = _norm(quote), _norm(page_text)
    if len(q) < 4 or not p:
        return False
    if q in p:
        return True
    # Words may be fuzzy (OCR, hyphenation); figures may not: '14.4%' is 90% similar to '12.4%'.
    return fuzz.partial_ratio(q, p) >= threshold and numbers(q) <= numbers(p)


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
