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
from .parser import candidate_pages, detect_unit, page_basis, parse_page, sane, statement_pages

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
          snapshot: dict | None = None, cross: dict | None = None, other: dict | None = None) -> dict:
    """other: the parser's reading of the other basis from the same PDF ({"basis", "revenue", "pat", "page"} in
    ₹ crore), used to explain a mismatch with BSE's summary, which is one basis (usually standalone)."""
    conf, notes, checks = "high", [], []
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
    if other and rev and not (0.05 <= abs(other.get("revenue") or 0) / abs(rev) <= 1.5):
        other = None   # not plausibly this company's other statement (a misread): never quoted, never used
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
            alt = bse_check({"revenue": {"current_quarter": (other or {}).get("revenue")},
                             "pat": {"current_quarter": (other or {}).get("pat")}}, snapshot, period) if other else None
            rev_only = bool(other and other.get("pat") is None and alt and alt.get("available")
                            and abs(other["revenue"] - alt["bse_revenue"]) <= 0.0001 * abs(alt["bse_revenue"]))
            if rev_only:
                check["explained_by_basis"] = f"{other['basis']} (revenue)"
                checks.append(f"BSE's summary is the {other['basis']} statement: FINN's {other['basis']} revenue from the same "
                              f"PDF (₹{other['revenue']:,.2f} cr, page {other['page']}) matches BSE's exactly; that statement's "
                              f"PAT line could not be read. The {basis} figures shown come from the same filing")
            elif alt and alt.get("match"):
                check["explained_by_basis"] = other["basis"]
                gap = max(abs(other["revenue"] - alt["bse_revenue"]) / abs(alt["bse_revenue"]),
                          abs(other["pat"] - alt["bse_net_profit"]) / max(abs(alt["bse_net_profit"]), 1e-9))
                how = "matches it exactly" if gap <= 0.0001 else f"is within {gap:.1%} of it"
                checks.append(f"BSE's summary is the {other['basis']} statement (revenue ₹{alt['bse_revenue']:,.2f} cr, PAT "
                              f"₹{alt['bse_net_profit']:,.2f} cr); FINN's {other['basis']} reading of the same PDF "
                              f"(₹{other['revenue']:,.2f} cr, ₹{other['pat']:,.2f} cr, page {other['page']}) {how}. "
                              f"The {basis} figures shown come from the same filing")
            else:
                why = []
                if other:
                    pat_txt = f"PAT ₹{other['pat']:,.2f} cr" if other.get("pat") is not None else "PAT not readable"
                    why.append(f"FINN's {other['basis']} reading is revenue ₹{other['revenue']:,.2f} cr, {pat_txt}")
                notes.append(f"differs from BSE's summary (BSE revenue ₹{check['bse_revenue']:,.2f} cr, PAT "
                             f"₹{(check.get('bse_net_profit') or 0):,.2f} cr" + ("; " + "; ".join(why) if why else "") + ")")
    if cross:
        for m in ("revenue", "pat"):
            a, b = norm.get(m, {}).get("current_quarter"), cross.get(m)
            if a is not None and b is not None and abs(a - b) > max(0.01 * abs(b), 0.05):
                if bse_agrees:  # BSE's independent figures side with these values: the parser misread
                    checks.append(f"{m}: the parser read a different figure; BSE's own figures confirm this one")
                else:
                    conf = "low"
                    notes.append(f"{m}: model and parser disagree")
    if bse_agrees:
        checks.append("revenue and PAT match BSE's structured figures")
    pat_row = next((t for t in table if t["metric"] == "pat" and t["found"]), None)
    return {"ok": True, "kind": "results", "period": period, "basis": basis, "unit_reported": unit, "unit": "₹ crore",
            "confidence": conf, "notes": notes, "checks": checks, "source": source, "table": table, "bse_check": check,
            "pat_yoy": pat_row["yoy"] if pat_row else None,
            # every line read for the current quarter, ₹ crore (feeds the margin bridge chart)
            "lines": {m: v.get("current_quarter") for m, v in norm.items() if v.get("current_quarter") is not None},
            "tax_gap": None if tax_gap is None else round(tax_gap, 3)}


def period_from_text(text: str) -> str | None:
    p = fiscal.parse_period(text)
    if p and p.q:
        return p.label()
    if p and p.h:  # "quarter and half year ended 30 September 2026": the quarter is Q2 (H1) or Q4 (H2)
        return fiscal.Period(p.fy, 2 if p.h == 1 else 4).label()
    return None


def parser_all(pages: list[str], revenue_only: dict | None = None) -> dict[str, tuple[dict, str, int]]:
    """basis -> (metrics, unit, page) for the first sane P&L the parser can read on each basis. A table that runs
    onto the next page is read as the page plus the next one. If `revenue_only` is given, statements where only
    revenue could be read are added to it (basis -> (revenue, unit, page)), to explain a basis mismatch with BSE."""
    out = {}
    revenue_only = {} if revenue_only is None else revenue_only
    for p in statement_pages(pages):
        texts = [pages[p - 1]] + ([pages[p - 1] + "\n" + pages[p]] if p < len(pages) else [])
        for text in texts:
            parsed = parse_page(text)
            unit = detect_unit(pages[p - 1]) or detect_unit(text)
            basis = page_basis(pages[p - 1]) or "standalone"
            if basis not in out and parsed.get("revenue") and parsed.get("pat") and sane(parsed):
                out[basis] = ({k: dict(v, page=p) for k, v in parsed.items()}, unit, p)
                break
            if parsed.get("revenue") and unit and basis not in revenue_only:
                revenue_only[basis] = (parsed["revenue"]["current_quarter"], unit, p)
        if len(out) == 2:
            break
    return out


UNIT_GUESSES = ("crore", "million", "lakh", "thousand")


def infer_units(found: dict, snapshot: dict | None, period: str | None) -> str | None:
    """A statement whose unit is not printed (a header graphic): the scale at which one basis' revenue AND PAT equal
    BSE's own figures to within 0.05% is the document's unit. Returns the unit (and fills it in), or None."""
    if not snapshot or not period or all(u for _, u, _ in found.values()):
        return None
    rb, pb = snapshot.get("revenue", {}).get(period), snapshot.get("net_profit", {}).get(period)
    if not rb or pb is None:
        return None
    for basis, (m, unit, page) in found.items():
        if unit:
            continue
        for u in UNIT_GUESSES:
            rev, pat = fiscal.to_crore(m["revenue"]["current_quarter"], u), fiscal.to_crore(m["pat"]["current_quarter"], u)
            if rev and abs(rev - rb) <= 0.0005 * abs(rb) and abs(pat - pb) <= max(0.0005 * abs(pb), 0.01):
                for b, (mm, uu, pp) in list(found.items()):
                    found[b] = (mm, uu or u, pp)
                return u
    return None


def bse_summary_card(snapshot: dict | None, period: str | None, reason: str) -> dict | None:
    """When the PDF cannot be read reliably (a scan, or OCR garbage), BSE's own structured figures for the quarter:
    revenue and net profit, with QoQ from BSE's previous quarter. Labelled as BSE's, never as FINN's reading."""
    if not snapshot or not period:
        return None
    rev = snapshot.get("revenue", {}).get(period)
    pat = snapshot.get("net_profit", {}).get(period)
    if rev is None or pat is None:
        return None
    prev = fiscal.parse_period(period)
    prev_label = None
    if prev and prev.q:
        q, fy = (prev.q - 1, prev.fy) if prev.q > 1 else (4, prev.fy - 1)
        prev_label = fiscal.Period(fy, q).label()
    table = []
    for metric, key in (("revenue", "revenue"), ("pat", "net_profit")):
        cur = snapshot[key].get(period)
        before = snapshot[key].get(prev_label) if prev_label else None
        table.append({"metric": metric, "found": True, "current": cur, "previous": before, "year_ago": None,
                      "qoq": pct_change(cur, before), "yoy": None, "page": None, "quote": None, "derived": None})
    table += [{"metric": "ebitda", "found": False}, {"metric": "pbt", "found": False}]
    return {"ok": True, "kind": "results", "period": period, "basis": "as published by BSE", "unit": "₹ crore",
            "confidence": "high", "notes": [], "source": "BSE's structured results summary",
            "checks": [f"FINN could not read the filing itself ({reason}); these are BSE's own figures for {period}. "
                       "Year-on-year, EBITDA and PBT are not in BSE's summary."],
            "table": table, "bse_check": None, "from_bse_summary": True, "pdf_reason": reason}


def _other(found: dict, shown_basis: str | None, revenue_only: dict | None = None) -> dict | None:
    """The parser's reading of the basis not shown, in ₹ crore, for explaining a mismatch with BSE."""
    for basis, (m, unit, page) in found.items():
        if basis != shown_basis:
            return {"basis": basis, "page": page, "revenue": fiscal.to_crore(m["revenue"]["current_quarter"], unit),
                    "pat": fiscal.to_crore(m["pat"]["current_quarter"], unit)}
    for basis, (rev, unit, page) in (revenue_only or {}).items():
        if basis != shown_basis:
            return {"basis": basis, "page": page, "revenue": fiscal.to_crore(rev, unit), "pat": None}
    return None


def extract(pages: list[str], subject: str = "", snapshot: dict | None = None, use_llm: bool = True,
            budget_key: str | None = None, filed_on=None) -> dict:
    period = period_from_text(subject) or period_from_text(" ".join(pages[:2])[:3000])
    if not period and filed_on:
        # Results are filed after the quarter closes, so the last completed quarter on the filing date is the period.
        period = fiscal.last_completed_quarter(filed_on).label()
    if not statement_pages(pages):
        return {"ok": False, "reason": "no P&L statement found in the PDF", "period": period}
    rev_only: dict = {}
    found = parser_all(pages, rev_only)
    inferred = infer_units(found, snapshot, period)
    found = {b: v for b, v in found.items() if v[1]}           # a statement whose unit is still unknown is unusable
    det, det_unit, det_basis = {}, None, None
    for b in ("consolidated", "standalone"):
        if b in found:
            det, det_unit, _ = found[b]
            det_basis = b
            break
    # any page headed as consolidated results, even one the parser cannot read: then standalone is not enough
    has_consolidated = any(page_basis(t) == "consolidated" and "result" in t[:1500].lower() for t in pages)
    if det:
        built = build(det, det_unit, det_basis, "parser", period, snapshot, other=_other(found, det_basis, rev_only))
        if inferred and built.get("ok"):
            built["checks"].append(f"the statement does not print its unit; FINN took ₹ {inferred} because at that "
                                   "scale its figures equal BSE's own to within 0.05%")
        chk = built.get("bse_check") or {}
        # The model-free shortcut only when the parser read the statement we would show anyway (consolidated, or
        # standalone because there is no consolidated one) and BSE's figures confirm the reading, on this basis or,
        # for BSE's standalone summary, on the standalone statement of the same PDF.
        if (built.get("ok") and built["confidence"] != "low" and (chk.get("match") or chk.get("explained_by_basis"))
                and (det_basis == "consolidated" or not has_consolidated)):
            built["source"] = "parser, checked against BSE's structured figures"
            return built  # verified twice without a model call
    det_cross = None
    if det and det_unit:
        det_cross = {m: fiscal.to_crore(v["current_quarter"], det_unit) for m, v in det.items()}
    if use_llm:
        try:
            from ..llm import router
            sp = statement_pages(pages)
            cons = [p for p in sp if page_basis(pages[p - 1]) == "consolidated"]
            cands = (cons + [p for p in sp if p not in cons])[:3]        # the consolidated statement first
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
                                cross=det_cross if det_basis == basis else None, other=_other(found, basis, rev_only))
                    out["dropped_unverified"] = len(rows) - len([x for x in rows if x in kept])
                    if out.get("ok") and basis == "standalone" and has_consolidated:
                        out["checks"].append("the filing also has consolidated results, which FINN could not read; "
                                             "standalone shown")
                    return out
        except Exception as e:  # noqa: BLE001
            log.warning("model results extraction failed: %s", e)
    if det:
        out = build(det, det_unit, det_basis, "parser", period, snapshot, other=_other(found, det_basis, rev_only))
        if out.get("ok") and det_basis == "standalone" and has_consolidated:
            out["checks"].append("the filing also has consolidated results, which FINN could not read; standalone shown")
        return out
    return {"ok": False, "reason": "figures could not be read and verified", "period": period}
