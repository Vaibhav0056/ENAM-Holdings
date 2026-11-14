"""Management Credibility Ledger: pure Python, no I/O, unit-tested.

promise + reported actuals -> verdict (delivered | partial | missed | pending | unverifiable) -> credibility score,
vagueness %, per-quarter trend and guidance drift. Ported from the Step 1-8 engine (its matching rules were each fixed
against real rows) onto finn.fiscal periods.

Credibility (spec §5): over measurable promises whose horizon has passed, sum(w_q * v) / sum(w_q), v in {1, 0.5, 0},
w = 0.8 ** (quarters between the call the promise was made on and the latest call). Shown only with `measured n`.
"""
from __future__ import annotations

import re
from collections import defaultdict

from rapidfuzz import fuzz

from ..fiscal import Period, parse_period

VERDICTS = ("delivered", "partial", "missed", "pending", "unverifiable")
VALUE = {"delivered": 1.0, "partial": 0.5, "missed": 0.0}
DECAY = 0.8
MIN_MEASURED = 3        # below this a score is an anecdote, not a track record

# ---------- metrics and units ----------

_GENERIC = {"growth", "rate", "increase", "level", "share", "contribution", "guidance", "target", "ratio", "percentage",
            "cycle", "day", "steady", "state", "long", "term", "medium", "annual", "run", "net", "gross", "from",
            "investment", "amount", "value", "total", "budget", "spend", "expenditure", "plan", "program", "programme"}


def metric_tokens(m: str) -> list[str]:
    m = (m or "").lower().replace("capital expenditure", "capex").replace("&", " and ").replace("-", " ").replace("/", " ")
    m = re.sub(r"\b(the|of|our|overall|total|consolidated|company|business|yoy|y o y|year on year|in|for|to|a)\b", " ", m)
    toks = []
    for t in re.findall(r"[a-z0-9]+", m):
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        toks.append(t)
    return toks


def metric_match(a: str, b: str) -> bool:
    """Same metric only: fuzzy-equal AND no segment word on one side only ('FMEG EBITDA margin' is not the
    company's 'EBITDA margin')."""
    ta, tb = metric_tokens(a), metric_tokens(b)
    if not ta or not tb:
        return False
    if (set(ta) ^ set(tb)) - _GENERIC - {"and"}:
        return False
    # A share of revenue and a growth rate are different measures even though both words are generic
    # ("export revenue contribution" is not "export revenue growth").
    share, growth = {"contribution", "share", "mix", "salience"}, {"growth", "increase", "cagr"}
    if (share & set(ta) and growth & set(tb) and not share & set(tb)) or (share & set(tb) and growth & set(ta) and not share & set(ta)):
        return False
    return fuzz.token_sort_ratio(" ".join(ta), " ".join(tb)) >= 70 or set(ta) - _GENERIC == set(tb) - _GENERIC


def money_factor(u: str | None) -> float | None:
    """₹ crore per unit for money units (crore, lakh, million, billion, 'lakh crore'), else None."""
    low = (u or "").lower()
    if "%" in low or "percent" in low or "bps" in low:
        return None
    if "lakh crore" in low or "lakh cr" in low:
        return 1e5
    from ..fiscal import unit_factor
    return unit_factor(low)


def unit_key(u: str | None) -> str:
    u = (u or "").lower().strip()
    if money_factor(u):
        return "crore"   # all money is compared in ₹ crore
    if "%" in u or "percent" in u:
        return "%"
    if "bps" in u or "basis" in u:
        return "bps"
    if "crore" in u or u in {"cr", "rs cr", "inr cr", "₹ cr"}:
        return "crore"
    return u


def period(text: str | None) -> Period | None:
    """A horizon like 'FY26-FY28' ends at its last year; parse_period already takes the last FY mentioned."""
    if not text:
        return None
    t = re.sub(r"(fy\s*'?\d{2,4})\s*(?:-|to|–)\s*(fy\s*'?\d{2,4})", r"\2", text, flags=re.I)
    return parse_period(t)


# ---------- verdicts ----------

LOWER_IS_BETTER = re.compile(r"debt|cost|working capital|days|leverage|attrition|npa|gnpa|credit cost", re.I)
MORE_IS_BETTER_OVERRIDE = re.compile(r"reduc|saving|repay", re.I)


def verdict_for(p: dict, actual: dict | None, latest: Period | None) -> str:
    lo, hi = p.get("target_low"), p.get("target_high")
    if lo is None and hi is None:
        return "unverifiable"
    hz = period(p.get("horizon"))
    if actual is None:
        if hz is None or latest is None or latest < hz or latest == hz:
            return "pending"   # horizon not over yet (or open-ended)
        return "unverifiable"  # horizon passed but nothing reported for it
    if unit_key(actual.get("unit")) != unit_key(p.get("unit")):
        return "unverifiable"
    v = actual["value"]
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    fa, fp = money_factor(actual.get("unit")), money_factor(p.get("unit"))
    if fa and fp:  # e.g. promised "₹1,200-1,600 crore", reported "6.1 billion"
        v, lo, hi = v * fa, lo * fp, hi * fp
    if LOWER_IS_BETTER.search(p["metric"]) and not MORE_IS_BETTER_OVERRIDE.search(p["metric"]):
        if v <= hi:
            return "delivered"
        return "partial" if v <= hi * 1.1 else "missed"
    if v >= lo:
        return "delivered"
    return "partial" if v >= lo * 0.9 else "missed"


def latest_period(promises: list[dict], actuals: list[dict]) -> Period | None:
    ps = [period(a.get("period")) for a in actuals] + [period(p.get("said_in")) for p in promises]
    ps = [p for p in ps if p]
    return max(ps) if ps else None


def match(promises: list[dict], actuals: list[dict]) -> list[dict]:
    """One row per promise: the actual for exactly the promised period (Q4 is not FY, Q2 is not H1), else the latest
    reported figure for the same metric as progress, with an on-track read for ratio-like units only."""
    latest = latest_period(promises, actuals)
    rows = []
    for p in promises:
        hz = period(p.get("horizon"))
        cands = [a for a in actuals if metric_match(p["metric"], a["metric"])]
        same_unit = lambda a: unit_key(a.get("unit")) == unit_key(p.get("unit"))  # noqa: E731
        exact = [a for a in cands if hz and period(a.get("period")) == hz]
        actual = max(exact, key=same_unit) if exact else None
        progress = None
        if not actual:
            dated = [a for a in cands if period(a.get("period")) and (hz is None or not (hz < period(a["period"])))]
            progress = max(dated, key=lambda a: (same_unit(a), period(a["period"]))) if dated else None
        v = verdict_for(p, actual, latest)
        tracking = None
        ratio_like = unit_key(p.get("unit")) in {"%", "bps", "x"} or "day" in (p.get("unit") or "").lower()
        if progress and v == "pending" and ratio_like:
            t = verdict_for(dict(p, horizon=progress["period"]), progress, latest)
            tracking = {"delivered": "on track", "partial": "slightly behind", "missed": "behind"}.get(t)
        rows.append({"promise": p, "actual": actual, "progress": progress, "verdict": v, "tracking": tracking})
    return rows


def _bounds(p):
    """(low, high) with money converted to ₹ crore, so "₹1,200-1,600 crore" and "₹12-16 billion" are the same target."""
    lo, hi = p.get("target_low"), p.get("target_high")
    lo, hi = (hi if lo is None else lo, lo if hi is None else hi)
    f = money_factor(p.get("unit"))
    return (round(lo * f, 6), round(hi * f, 6)) if f and lo is not None else (lo, hi)


def same_promise(a: dict, b: dict) -> bool:
    ha, hb = period(a.get("horizon")), period(b.get("horizon"))
    return (_bounds(a) == _bounds(b) and unit_key(a.get("unit")) == unit_key(b.get("unit"))
            and (ha == hb if ha or hb else (a.get("horizon") or "").strip().lower() == (b.get("horizon") or "").strip().lower())
            and metric_match(a["metric"], b["metric"]))


def dedupe(promises: list[dict]) -> list[dict]:
    """A promise repeated on several calls is one promise, counted once; keep its latest statement and remember
    how often it was repeated."""
    kept: list[dict] = []
    for p in sorted(promises, key=lambda p: period(p.get("said_in")) or Period(0), reverse=True):
        twin = next((k for k in kept if same_promise(p, k)), None)
        if twin:
            twin.setdefault("also_said_in", []).append(p.get("said_in"))
        else:
            kept.append(dict(p))
    return kept


# ---------- scores ----------

def quarters_between(a: Period, b: Period) -> int:
    return max(0, round((b.end() - a.end()).days / 91.3))


def credibility(rows: list[dict]) -> dict:
    """Recency-weighted score over measurable promises whose horizon has passed (delivered/partial/missed)."""
    measured = [r for r in rows if r["verdict"] in VALUE]
    total = len(rows)
    vague = sum(r["verdict"] == "unverifiable" for r in rows)
    said = [period(r["promise"].get("said_in")) for r in measured]
    ref = max([s for s in said if s], default=None)
    num = den = 0.0
    trend: dict[str, list[float]] = defaultdict(list)
    for r, s in zip(measured, said):
        w = DECAY ** quarters_between(s, ref) if (s and ref) else DECAY ** 4
        num += w * VALUE[r["verdict"]]
        den += w
        trend[s.label() if s else "unknown"].append(VALUE[r["verdict"]])
    score = round(100 * num / den) if den and len(measured) >= MIN_MEASURED else None
    return {
        "score": score, "measured": len(measured), "total": total,
        "vagueness_pct": round(100 * vague / total) if total else None,
        "counts": {v: sum(r["verdict"] == v for r in rows) for v in VERDICTS},
        "trend": [{"said_in": k, "n": len(v), "score": round(100 * sum(v) / len(v))}
                  for k, v in sorted(trend.items(), key=lambda kv: period(kv[0]) or Period(0))],
        "method": "Σ 0.8^age · value / Σ 0.8^age over promises whose horizon has passed; delivered 1, partial 0.5, missed 0",
        "min_measured": MIN_MEASURED,
    }


def drift(promises: list[dict]) -> list[dict]:
    """How guidance for one metric and horizon moved from call to call: raised, cut, narrowed, widened, maintained,
    or dropped (said on an earlier call, absent from every later call that discussed the company)."""
    calls = sorted({p.get("said_in") for p in promises if period(p.get("said_in"))}, key=period)
    groups: list[list[dict]] = []
    for p in sorted(promises, key=lambda p: period(p.get("said_in")) or Period(0)):
        if p.get("target_low") is None and p.get("target_high") is None:
            continue
        g = next((g for g in groups if metric_match(g[0]["metric"], p["metric"])
                  and period(g[0].get("horizon")) == period(p.get("horizon")) and period(p.get("horizon"))
                  and unit_key(g[0].get("unit")) == unit_key(p.get("unit"))), None)
        (g.append(p) if g else groups.append([p]))
    out = []
    for g in groups:
        steps = []
        for a, b in zip(g, g[1:]):
            (alo, ahi), (blo, bhi) = _bounds(a), _bounds(b)
            mid_a, mid_b = (alo + ahi) / 2, (blo + bhi) / 2
            lower_better = bool(LOWER_IS_BETTER.search(a["metric"]))
            if abs(mid_b - mid_a) <= 1e-6 * max(abs(mid_a), 1):
                kind = "narrowed" if (bhi - blo) < (ahi - alo) else "widened" if (bhi - blo) > (ahi - alo) else "maintained"
            elif (mid_b > mid_a) != lower_better:
                kind = "raised"
            else:
                kind = "cut"
            steps.append({"from": a.get("said_in"), "to": b.get("said_in"), "change": kind,
                          "before": a.get("target_text"), "after": b.get("target_text"),
                          "before_mid": round(mid_a, 2), "after_mid": round(mid_b, 2)})
        last_said = period(g[-1].get("said_in"))
        later_calls = [c for c in calls if last_said and last_said < period(c)]
        hz = period(g[-1].get("horizon"))
        # Dropped = a target management repeated on at least two calls, then stopped mentioning on every later
        # call, while its horizon was still open. One mention that later calls word differently is not a drop.
        said_on = {period(p.get('said_in')) for p in g} | {period(x) for p in g for x in p.get('also_said_in', [])}
        dropped = (len({x for x in said_on if x}) >= 2 and bool(later_calls)
                   and (hz is None or not (hz < period(later_calls[-1]))))
        if steps or dropped:
            out.append({"metric": g[-1]["metric"], "horizon": g[-1].get("horizon"), "unit": g[-1].get("unit"),
                        "mid_unit": "₹ crore" if money_factor(g[-1].get("unit")) else g[-1].get("unit"),
                        "steps": steps, "dropped": dropped,
                        "last_said_in": g[-1].get("said_in"), "promise_ids": [p.get("id") for p in g]})
    return out
