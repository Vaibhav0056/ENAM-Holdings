"""Pure-Python maths for deep dives: cash quality, balance sheet, hedge-meter, language diff, worry map, peer compare.
Every number a deep dive shows is computed here from verified, quoted inputs."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

# ---------- hedge-meter (spec §5) ----------

HEDGES = ["challenging", "subject to", "we expect", "headwind", "headwinds", "cautious", "uncertain", "uncertainty",
          "may", "could", "might", "volatile", "volatility", "difficult", "softness", "soft", "pressure", "pressures",
          "slowdown", "moderate", "moderation", "tough", "weak", "weakness", "temporary", "transitory", "wait and watch",
          "too early", "difficult to say", "hard to predict", "visibility is limited", "low visibility"]
_HEDGE_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(h) for h in HEDGES), key=len, reverse=True)) + r")\b", re.I)


def hedge_rate(texts: list[str]) -> dict:
    words = sum(len(t.split()) for t in texts)
    hits = Counter(m.group(1).lower() for t in texts for m in _HEDGE_RE.finditer(t))
    n = sum(hits.values())
    return {"words": words, "hedges": n, "per_1000_words": round(1000 * n / words, 2) if words else None,
            "top": hits.most_common(5)}


# ---------- cash quality and balance sheet ----------

def ratio(a, b, digits=2):
    if a is None or b in (None, 0):
        return None
    return round(a / b, digits)


def days(balance, flow):
    """Balance-sheet days: balance / annual flow × 365."""
    if balance is None or flow in (None, 0):
        return None
    return round(balance / flow * 365, 1)


def cash_quality(f: dict) -> dict:
    """f: {item: value in ₹ crore} for one year. OCF/PAT, FCF, working-capital days, cash conversion cycle."""
    ocf, pat, capex = f.get("ocf"), f.get("pat"), f.get("capex")
    fcf = None if ocf is None or capex is None else round(ocf - abs(capex), 2)
    dso, dio, dpo = days(f.get("receivables"), f.get("revenue")), days(f.get("inventories"), f.get("cogs") or f.get("revenue")), \
        days(f.get("payables"), f.get("cogs") or f.get("revenue"))
    ccc = None if None in (dso, dio, dpo) else round(dso + dio - dpo, 1)
    checks = []
    for name, v in (("receivable days", dso), ("inventory days", dio), ("payable days", dpo)):
        if v is not None and not 7 <= v <= 365:
            checks.append(f"{name} of {v} is unusual: check the quoted line is the total, not a sub-line")
    return {"ocf_to_pat": ratio(ocf, pat), "fcf": fcf, "fcf_to_pat": ratio(fcf, pat), "receivable_days": dso,
            "inventory_days": dio, "payable_days": dpo, "cash_conversion_cycle": ccc,
            "inventory_basis": "COGS" if f.get("cogs") else "revenue", "checks": checks}


def balance_sheet(f: dict) -> dict:
    debt = sum(v for v in (f.get("borrowings_long"), f.get("borrowings_short")) if v is not None) \
        if (f.get("borrowings_long") is not None or f.get("borrowings_short") is not None) else None
    cash = sum(v for v in (f.get("cash"), f.get("current_investments")) if v is not None) \
        if (f.get("cash") is not None or f.get("current_investments") is not None) else None
    net_debt = None if debt is None or cash is None else round(debt - cash, 2)
    ebitda = f.get("ebitda")
    return {"gross_debt": debt, "cash_and_liquid_investments": cash, "net_debt": net_debt,
            "net_debt_to_ebitda": ratio(net_debt, ebitda), "debt_to_equity": ratio(debt, f.get("equity")),
            "interest_cover": ratio(ebitda, f.get("finance_costs"), 1),
            "contingent_liabilities_to_equity": ratio(f.get("contingent_liabilities"), f.get("equity"), 3)}


def yoy(cur: dict, prev: dict) -> dict:
    out = {}
    for k, v in cur.items():
        p = prev.get(k)
        if isinstance(v, (int, float)) and isinstance(p, (int, float)) and p:
            out[k] = round((v - p) / abs(p) * 100, 1)
    return out


# ---------- language diff ----------

def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return num / den if den else 0.0


QUANT = re.compile(r"\d|double[- ]digit|single[- ]digit|mid[- ]teens|high[- ]teens|\bx\b", re.I)


def language_diff(older: list[dict], newer: list[dict], threshold: float = 0.8, limit: int = 8) -> dict:
    """older/newer: management statements [{text, embedding, page, ...}]. A statement with no counterpart above the
    similarity threshold on the other call is 'dropped' (older) or 'new' (newer). Quantified statements first."""
    def unmatched(a, b):
        out = []
        for x in a:
            best = max((cosine(x["embedding"], y["embedding"]) for y in b), default=0.0)
            if best < threshold:
                out.append({**{k: v for k, v in x.items() if k != "embedding"}, "best_match": round(best, 3)})
        out.sort(key=lambda x: (not QUANT.search(x["text"]), x["best_match"]))
        return out[:limit]
    return {"dropped": unmatched(older, newer), "new": unmatched(newer, older), "threshold": threshold}


# ---------- worry map ----------

def worry_grid(labels: list[dict]) -> dict:
    """labels: [{quarter, topic}] -> {topics: [...], quarters: [...], counts: {topic: {quarter: n}}, rising: [...]}"""
    counts = defaultdict(Counter)
    quarters = []
    for x in labels:
        counts[x["topic"]][x["quarter"]] += 1
        if x["quarter"] not in quarters:
            quarters.append(x["quarter"])
    from ..research.ledger import period
    quarters.sort(key=lambda q: period(q) or 0)
    topics = sorted(counts, key=lambda t: -sum(counts[t].values()))
    rising = []
    if len(quarters) >= 2:
        last, prev = quarters[-1], quarters[:-1]
        for t in topics:
            avg_prev = sum(counts[t][q] for q in prev) / len(prev)
            if counts[t][last] >= 2 and counts[t][last] > 1.5 * avg_prev:
                rising.append(t)
    return {"topics": topics, "quarters": quarters, "counts": {t: dict(counts[t]) for t in topics}, "rising": rising}


# ---------- peer compare ----------

def compare_ledgers(a: dict, b: dict) -> dict:
    """Two research reports side by side: credibility, verdict mix, vagueness, drift count."""
    def row(r):
        c = r["credibility"]
        return {"symbol": r["symbol"], "name": r["name"], "score": c["score"], "measured": c["measured"], "total": c["total"],
                "vagueness_pct": c["vagueness_pct"], "counts": c["counts"],
                "drift_items": len(r.get("drift") or []), "dropped_guidance": sum(1 for d in r.get("drift") or [] if d.get("dropped"))}
    return {"rows": [row(a), row(b)]}
