"""Results card: Revenue, EBITDA, PBT, PAT with QoQ and YoY, in ₹ crore, with basis, units, sanity checks and a
quote + page behind every figure.

Order of trust (cheapest verified path first):
1. Deterministic parser over pdfplumber text (Step 1-8 parser, proven on these PDFs). If its figures pass the P&L
   sanity checks AND match BSE's own structured snapshot for the quarter, they are verified by two independent
   readings and no model call is made.
2. Otherwise the cheap model reads the statement (consolidated preferred) and every row's quote is verified on its
   page; the parser, when it agrees on the basis, cross-checks it.
Python does every number after extraction: units → ₹ crore, EBITDA = PBT + finance costs + depreciation − other
income (reported EBITDA wins), QoQ/YoY, and the sanity flags.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from .. import fiscal
from ..grounding import verify
from .parser import candidate_pages, detect_unit, page_basis, parse_page, sane

log = logging.getLogger("finn.enrich.results")
CORE = ["revenue", "ebitda", "pbt", "pat"]
QCOLS = ("current_quarter", "previous_quarter", "same_quarter_last_year")
TAX_TOLERANCE = 0.05
BSE_TOLERANCE = 0.01


def pct_change(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100.0, 1)


def ebitda_from(pbt, fin, dep, oi):
    if None in (pbt, fin, dep):
        return None
    return round(pbt + fin + dep - (oi or 0.0), 4)


class MetricRow(BaseModel):
    metric: Literal["revenue", "other_income", "finance_costs", "depreciation", "pbt", "tax", "pat", "ebitda"]
    current_quarter: Optional[float]
    previous_quarter: Optional[float]
    same_quarter_last_year: Optional[float]
    unit: str
    basis: Literal["consolidated", "standalone"]
    page: int
    quote: str


class ResultsExtraction(BaseModel):
    period: Optional[str] = None
    rows: list[MetricRow]


def normalise(metrics: dict, unit: str) -> dict:
    """metric -> {current_quarter, previous_quarter, same_quarter_last_year (₹ crore), page, quote}."""
    out = {}
    for m, v in metrics.items():
        out[m] = {k: fiscal.to_crore(v.get(k), unit) for k in QCOLS}
        out[m].update(page=v.get("page"), quote=v.get("quote"))
    return out


def bse_check(norm: dict, snapshot: dict | None, period: str | None) -> dict | None:
    """Compare the current quarter's revenue and PAT with BSE's own structured figures."""
    if not snapshot or not period:
        return None
    rev_b, pat_b = snapshot.get("revenue", {}).get(period), snapshot.get("net_profit", {}).get(period)
    rev, pat = norm.get("revenue", {}).get("current_quarter"), norm.get("pat", {}).get("current_quarter")
    if rev_b is None or rev is None:
        return {"period": period, "available": False}
    rev_ok = abs(rev - rev_b) <= BSE_TOLERANCE * abs(rev_b)
    pat_ok = pat is not None and pat_b is not None and abs(pat - pat_b) <= max(BSE_TOLERANCE * abs(pat_b), 0.5)
    return {"period": period, "available": True, "bse_revenue": rev_b, "bse_net_profit": pat_b,
            "revenue_match": rev_ok, "pat_match": pat_ok, "match": rev_ok and pat_ok}


def build(metrics: dict, unit: str, basis: str, source: str, period: str | None = None,
          snapshot: dict | None = None, cross: dict | None = None) -> dict:
    conf, notes = "high", []
    if fiscal.unit_factor(unit) is None:
        return {"ok": False, "reason": f"unknown unit '{unit}'", "confidence": "low"}
    norm = normalise(metrics, unit)
    if "ebitda" not in norm and all(k in norm for k in ("pbt", "finance_costs", "depreciation")):
        e = {k: ebitda_from(norm["pbt"][k], norm["finance_costs"][k], norm["depreciation"][k],
                            norm.get("other_income", {}).get(k)) for k in QCOLS}
        e.update(page=norm["pbt"]["page"], quote=None, derived="PBT + finance costs + depreciation − other income")
        norm["ebitda"] = e
    table = []
    for m in CORE:
        v = norm.get(m)
        if not v or v.get("current_quarter") is None:
            table.append({"metric": m, "found": False})
            continue
        qoq, yoy = pct_change(v["current_quarter"], v["previous_quarter"]), pct_change(v["current_quarter"], v["same_quarter_last_year"])
        for label, ch in (("QoQ", qoq), ("YoY", yoy)):
            if ch is not None and abs(ch) > 300:
                conf = "low"
                notes.append(f"{m} {label} change beyond ±300%")
        table.append({"metric": m, "found": True, "current": v["current_quarter"], "previous": v["previous_quarter"],
                      "year_ago": v["same_quarter_last_year"], "qoq": qoq, "yoy": yoy, "page": v.get("page"),
                      "quote": v.get("quote"), "derived": v.get("derived")})
    rev = norm.get("revenue", {}).get("current_quarter")
    pat = norm.get("pat", {}).get("current_quarter")
    if rev is None or pat is None:
        conf = "low"
        notes.append("revenue or PAT not found")
    else:
        if not (1 <= abs(rev) <= 500_000):
            conf = "low"
            notes.append("revenue magnitude implausible for ₹ crore")
        if abs(pat) > abs(rev) * 1.5:
            conf = "low"
            notes.append("PAT larger than revenue")
    pbt, tax = norm.get("pbt", {}).get("current_quarter"), norm.get("tax", {}).get("current_quarter")
    tax_gap = None
    if None not in (pbt, tax, pat) and pat:
        tax_gap = abs(pbt - tax - pat) / abs(pat)
        if tax_gap >= TAX_TOLERANCE:
            # Consolidated statements add share of associates/JVs or exceptional items after tax; a gap is a flag,
            # not proof of a misread, so it lowers confidence one step.
            conf = "medium" if conf == "high" else conf
            notes.append(f"PBT − tax differs from PAT by {tax_gap:.0%} (associates, exceptional items or a misread)")
    ebitda_c, pbt_c = norm.get("ebitda", {}).get("current_quarter"), norm.get("pbt", {}).get("current_quarter")
    if rev and any(v is not None and v > rev for v in (ebitda_c, pbt_c)):
        conf = "low"
        notes.append("EBITDA or PBT larger than revenue")
    check = bse_check(norm, snapshot, period)
    bse_agrees = bool(check and check.get("match"))
    if check and check.get("available") and rev:
        ratio = check["bse_revenue"] / rev if rev else 0
        scale = next((f for f in (10, 100, 1000, 1e5, 1e7, 0.1, 0.01, 0.001) if abs(ratio / f - 1) < 0.02), None)
        if scale:
            # Off by an exact power of ten from BSE's own figure: a unit or digit-grouping misread ("533-402" read
            # as 533.402). Never shown: a wrong number is worse than none.
            return {"ok": False, "kind": "results", "period": period, "confidence": "low",
                    "reason": f"figures are {scale:g}× off BSE's own numbers (unit or digit misread); not shown",
                    "bse_check": check}
        if not check["match"]:
            notes.append("differs from BSE's snapshot (it may be on the other basis)")
    if cross:
        for m in ("revenue", "pat"):
            a, b = norm.get(m, {}).get("current_quarter"), cross.get(m)
            if a is not None and b is not None and abs(a - b) > max(0.01 * abs(b), 0.05):
                if bse_agrees:  # BSE's independent figures side with these values: the parser misread
                    notes.append(f"{m}: parser disagreed; BSE's figures confirm the extracted value")
                else:
                    conf = "low"
                    notes.append(f"{m}: model and parser disagree")
    if bse_agrees:
        notes.append("revenue and PAT match BSE's structured figures")
    pat_row = next((t for t in table if t["metric"] == "pat" and t["found"]), None)
    return {"ok": True, "kind": "results", "period": period, "basis": basis, "unit_reported": unit, "unit": "₹ crore",
            "confidence": conf, "notes": notes, "source": source, "table": table, "bse_check": check,
            "pat_yoy": pat_row["yoy"] if pat_row else None,
            "tax_gap": None if tax_gap is None else round(tax_gap, 3)}


def period_from_text(text: str) -> str | None:
    p = fiscal.parse_period(text)
    if p and p.q:
        return p.label()
    if p and p.h:  # "quarter and half year ended 30 September 2026": the quarter is Q2 (H1) or Q4 (H2)
        return fiscal.Period(p.fy, 2 if p.h == 1 else 4).label()
    return None


def parser_pass(pages: list[str]) -> tuple[dict, str | None, str | None]:
    for p in candidate_pages(pages)[:6]:
        parsed = parse_page(pages[p - 1])
        unit = detect_unit(pages[p - 1])
        if parsed.get("revenue") and parsed.get("pat") and unit and sane(parsed):
            return {k: dict(v, page=p) for k, v in parsed.items()}, unit, page_basis(pages[p - 1]) or "standalone"
    return {}, None, None


def extract(pages: list[str], subject: str = "", snapshot: dict | None = None, use_llm: bool = True,
            budget_key: str | None = None, filed_on=None) -> dict:
    period = period_from_text(subject) or period_from_text(" ".join(pages[:2])[:3000])
    if not period and filed_on:
        # Results are filed after the quarter closes, so the last completed quarter on the filing date is the period.
        period = fiscal.last_completed_quarter(filed_on).label()
    if not candidate_pages(pages):
        return {"ok": False, "reason": "no P&L statement found in the PDF", "period": period}
    det, det_unit, det_basis = parser_pass(pages)
    has_consolidated = any(page_basis(pages[p - 1]) == "consolidated" for p in candidate_pages(pages))
    if det:
        built = build(det, det_unit, det_basis, "parser", period, snapshot)
        # The model-free shortcut only when the parser read the statement we would show anyway: consolidated, or
        # standalone because the filing has no consolidated one (BSE's snapshot is often standalone, so a standalone
        # match must not crowd out the consolidated statement).
        if (built.get("ok") and built["confidence"] != "low" and (built.get("bse_check") or {}).get("match")
                and (det_basis == "consolidated" or not has_consolidated)):
            built["source"] = "parser, matched BSE's structured figures"
            return built  # verified twice without a model call
    det_cross = None
    if det and det_unit:
        det_cross = {m: fiscal.to_crore(v["current_quarter"], det_unit) for m, v in det.items()}
    if use_llm:
        try:
            from ..llm import router
            cands = candidate_pages(pages)[:3]
            doc = "\n\n".join(f"[PAGE {p}]\n{pages[p - 1][:9000]}" for p in sorted(cands))
            r = router.call("results_extract", "results_extract", {"document": doc}, ResultsExtraction, budget_key=budget_key)
            rows = [x.model_dump() for x in r.data.rows]
            kept = verify(rows, pages)
            if kept:
                basis = "consolidated" if any(x["basis"] == "consolidated" for x in kept) else "standalone"
                kept = [x for x in kept if x["basis"] == basis]
                metrics = {x["metric"]: x for x in kept}
                if "revenue" in metrics and "pat" in metrics:
                    out = build(metrics, kept[0]["unit"], basis, "model, quotes verified", period, snapshot,
                                cross=det_cross if det_basis == basis else None)
                    out["dropped_unverified"] = len(rows) - len([x for x in rows if x in kept])
                    return out
        except Exception as e:  # noqa: BLE001
            log.warning("model results extraction failed: %s", e)
    if det:
        return build(det, det_unit, det_basis, "parser", period, snapshot)
    return {"ok": False, "reason": "figures could not be read and verified", "period": period}
