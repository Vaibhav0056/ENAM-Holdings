"""Output guards for any generated prose (spec §7): the number check and the language guard.

- Number check: every number in generated prose must appear in the input evidence or a Python-computed value
  (rounding tolerated). A sentence that fails is removed and counted.
- Language guard: decision support only. Any sentence with buy / sell / hold / target-price style advice is removed.
"""
from __future__ import annotations

import re

_NUM = re.compile(r"(?<![A-Za-z0-9.])-?\d[\d,]*(?:\.\d+)?")
_ID = re.compile(r"\b[GASDPRQF]\d+\b")
ADVICE = re.compile(
    # advice phrasing, not the words themselves ("the company sells cables", "promoters hold 50%" are fine)
    r"\b(?:investors?|you|one|shareholders?) (?:should|could|may want to|might want to|must) "
    r"(?:buy|sell|hold|accumulate|exit|add|trim|avoid|book profits?)\b|"
    r"\b(?:buy|sell|hold|accumulate|reduce)(?: rating| recommendation| call)\b|"
    r"\b(?:buy|sell|hold|accumulate) (?:the|this) (?:stock|shares?)\b|"
    r"\b(?:strong buy|outperform|underperform|overweight|underweight|multibagger|price target|target price|"
    r"fair value of|upside of|downside of)\b|"
    r"\b(?:is|looks|appears) (?:a )?(?:cheap|expensive|undervalued|overvalued)\b", re.I)


def numbers(text: str) -> set[float]:
    return {v for v, _ in _numbers_with_places(text)}


def _numbers_with_places(text: str) -> set[tuple[float, int]]:
    out = set()
    for n in _NUM.findall(_ID.sub(" ", text or "")):
        n = n.replace(",", "").rstrip(".")
        try:
            out.add((round(float(n), 6), len(n.split(".")[1]) if "." in n else 0))
        except ValueError:
            continue
    return out


def allowed_numbers(*sources) -> set[float]:
    """Numbers present in the evidence (JSON strings, dicts, lists, numbers)."""
    import json
    out: set[float] = set()
    for s in sources:
        if isinstance(s, (int, float)):
            out.add(round(float(s), 6))
        else:
            out |= numbers(s if isinstance(s, str) else json.dumps(s, default=str))
    return out


def number_ok(n: float, allowed: set[float], places: int | None = None) -> bool:
    """Exact, or a rounding of an allowed value (to 0, 1 or 2 decimals), a count of 0-4, or a calendar year.
    ("Q2" and "FY27" never reach here: a number glued to letters isn't read as a number.)"""
    if n in allowed or (0 <= n <= 4 and float(n).is_integer()) or (2000 <= n <= 2100 and float(n).is_integer()):
        return True
    # a rounding of an allowed value at the precision the prose printed (12.35 -> "12.4", 1,234.6 -> "1,235")
    d = places if places is not None else (len(repr(n).split(".")[1]) if "." in repr(n) and not float(n).is_integer() else 0)
    return any(abs(a - n) <= 0.5 * 10 ** -d + 1e-9 for a in allowed)


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def scrub(text: str, allowed: set[float]) -> tuple[str, int, int]:
    """(clean text, sentences removed for unsupported numbers, sentences removed for advice language)."""
    keep, bad_num, bad_adv = [], 0, 0
    for s in sentences(text):
        if ADVICE.search(s):
            bad_adv += 1
        elif not all(number_ok(n, allowed, d) for n, d in _numbers_with_places(s)):
            bad_num += 1
        else:
            keep.append(s)
    return " ".join(keep).strip(), bad_num, bad_adv
