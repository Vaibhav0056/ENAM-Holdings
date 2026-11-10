"""Quarterly results extraction. LLM reads the table; Python does every number after that.

Primary path: CHEAP model extracts per-metric rows (with quote + page) from the P&L pages.
Fallback / cross-check: a deterministic line parser over the same pdfplumber text.
"""
import logging, re
from typing import Literal, Optional
from pydantic import BaseModel
from . import grounding, llm as llm_mod

log = logging.getLogger("finn.results")

UNIT_TO_CRORE = {"crore": 1.0, "lakh": 0.01, "million": 0.1, "billion": 100.0, "thousand": 0.0001, "rupees": 1e-7}

METRIC_LABELS = {
    "revenue": ["revenue from operations", "income from operations", "interest earned", "total revenue from operations"],
    "other_income": ["other income"],
    "finance_costs": ["finance costs", "finance cost", "interest expended", "interest expense"],
    "depreciation": ["depreciation and amortisation", "depreciation and amortization", "depreciation, amortisation",
                     "depreciation & amortisation", "depreciation"],
    "ebitda": ["ebitda"],
    "pbt": ["profit before tax", "profit before taxes", "profit before income tax"],
    "pat": ["net profit for the period", "profit for the period", "profit after tax", "net profit after tax",
            "profit for the quarter", "net profit for the quarter", "profit for the year"],
}
CORE = ["revenue", "ebitda", "pbt", "pat"]


# ---------- pure maths (tested) ----------

def to_crore(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    u = (unit or "").lower()
    for k, f in UNIT_TO_CRORE.items():
        if k in u or (k == "lakh" and "lac" in u) or (k == "million" and re.search(r"\bmn\b", u)):
            return round(value * f, 4)
    raise ValueError(f"unknown unit {unit!r}")


def pct_change(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100.0, 1)


def ebitda_from(pbt, fin, dep, oi):
    if None in (pbt, fin, dep):
        return None
    return pbt + fin + dep - (oi or 0.0)


def parse_number(tok: str) -> Optional[float]:
    t = tok.strip().replace("−", "-")
    if t in {"-", "–", "—", "--", "nil", "Nil"}:
        return 0.0
    m = re.fullmatch(r"(\()?(-)?([\d,]*\d(?:\.\d+)?)(\))?", t)
    if not m:
        return None
    v = float(m.group(3).replace(",", ""))
    return -v if (m.group(1) and m.group(4)) or m.group(2) else v


_UNIT_WORD = r"(crores?|cr\.?|lakhs?|lacs?|millions?|mn|billions?)"


def detect_unit(text: str) -> Optional[str]:
    """Finds the statement's unit in its header. OCR often mangles ₹ into f, t, ` or {, so we key off
    a short line or parenthesis containing 'in <unit>' / '<symbol> <unit>' rather than the symbol."""
    head = text[:3000].lower()
    pats = [rf"\(\s*[^\s()]{{0,3}}\s*(?:in\s+)?{_UNIT_WORD}\b",            # (₹ million) (f in Crores)
            rf"(?:₹|rs\.?|inr|`|rupees)\s*(?:in\s+)?{_UNIT_WORD}\b",       # ₹ in crore
            rf"\bin\s+(?:₹|rs\.?|inr|`|t|f)?\s*{_UNIT_WORD}\b",             # amounts in crore
            rf"\b(?:amount|figures)s?\s*(?:are\s*)?in\s*\S{{0,4}}\s*{_UNIT_WORD}\b"]
    for p in pats:
        m = re.search(p, head)
        if m:
            u = m.group(1).rstrip(".")
            return {"lac": "lakh", "lacs": "lakh", "mn": "million", "cr": "crore"}.get(u, u.rstrip("s"))
    return None


def page_basis(text: str) -> Optional[str]:
    head = text[:1200].upper()
    c, s = head.find("CONSOLIDATED"), head.find("STANDALONE")
    if c >= 0 and (s < 0 or c < s):
        return "consolidated"
    if s >= 0:
        return "standalone"
    return None


def candidate_pages(pages: list[str]) -> list[int]:
    """1-indexed pages that look like the P&L statement, consolidated first."""
    out = []
    for i, t in enumerate(pages, 1):
        low = t.lower()
        # OCR mangles labels ("Revenue from operalions"), so match the stem and the usual totals too.
        has_rev = (any(l in low for l in METRIC_LABELS["revenue"]) or "total income" in low or "total revenue" in low
                   or re.search(r"revenue\s+from\s+oper\w*", low) is not None)
        if has_rev and "profit before tax" in low.replace("(loss)", "").replace("/ ", "").replace("/", ""):
            out.append(i)
    def rank(i):
        t = pages[i - 1]
        dense = len(re.findall(r"\d[\d,]*\.\d{2}|\d{1,3}(?:,\d{2,3})+", t))
        return (page_basis(t) != "consolidated", detect_unit(t) is None, -min(dense, 60), i)
    return sorted(out, key=rank)


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line.lower().replace("/(loss)", "").replace("/ (loss)", "").replace("(loss)", "")
                  .replace("profit /", "profit").replace("profit/", "profit"))


def _row_numbers(toks: list[str]) -> list[float]:
    """Numbers on a table row, skipping row references like (9-10) / [(i)+(ii)] and leading note numbers."""
    toks = [t for t in toks if not re.fullmatch(r"[\[(]+[\divx]{1,4}\)?\s*[-+&]\s*\(?[\divx]{1,4}[\])]+|\[.*\]|\([a-z]{1,4}\)|\(\d\.\d\)", t)]
    nums = [(t, parse_number(t)) for t in toks]
    nums = [(t, n) for t, n in nums if n is not None]
    while len(nums) > 3 and re.fullmatch(r"\d{1,2}", nums[0][0]):
        nums = nums[1:]  # "Revenue from operations 1 30578.40 ..." -> drop the note ref "1"
    return [n for _, n in nums]


_SUBITEM = re.compile(r"^\s*(?:[-–•]|\(?[a-h]\)|\(?[ivx]{1,3}\))\s*\S")


def _sum_sublines(lines: list[str], idx: int, raw: str):
    """A label with no figures of its own ('Revenue from operations' then '- Sale of products 20,337 ...',
    '- Other operating revenues 277 ...') is the column-wise SUM of its sub-items. Python adds them."""
    rows, quote = [], [raw.strip()]
    for ln in lines[idx + 1: idx + 6]:
        if not _SUBITEM.match(ln):
            break
        nums = _row_numbers(ln.split())
        if len(nums) < 3:
            break
        rows.append(nums[:3])
        quote.append(ln.strip())
    if len(rows) < 2:
        return [], raw
    return [round(sum(r[i] for r in rows), 4) for i in range(3)], " / ".join(quote)


def parse_page(text: str) -> dict:
    """Deterministic: for each metric, take the first line starting with its label and read 3 numbers."""
    lines = text.splitlines()
    found = {}
    for idx, raw in enumerate(lines):
        line = _clean(raw)
        for metric, labels in METRIC_LABELS.items():
            if metric in found:
                continue
            for lab in labels:
                pos = line.find(lab)
                if pos < 0 or pos > 12:  # label must start the row (after a short sr. no.)
                    continue
                if metric == "pat" and ("attributable" in line or "comprehensive" in line):
                    continue
                if metric == "pbt" and "exceptional" in line and "before exceptional" in line:
                    continue
                toks = line[pos + len(lab):].split()
                nums = _row_numbers(toks)
                j = idx + 1
                while len(nums) < 3 and j < min(idx + 3, len(lines)):
                    nxt = lines[j].split()
                    if not nxt or parse_number(nxt[0]) is None:
                        break  # only pure-number continuation lines belong to this row
                    nums += _row_numbers(nxt)
                    j += 1
                if len(nums) < 3 and metric == "revenue":
                    nums, raw = _sum_sublines(lines, idx, raw)
                if len(nums) >= 3:
                    found[metric] = {"current_quarter": nums[0], "previous_quarter": nums[1],
                                     "same_quarter_last_year": nums[2], "quote": raw.strip()}
                break
    return found


def sane(m: dict) -> bool:
    """Reject parses that cannot be a P&L: revenue below profit, or revenue collapsing/exploding."""
    rev, pat, pbt = (m.get(k, {}).get("current_quarter") for k in ("revenue", "pat", "pbt"))
    if rev is None or rev <= 0:
        return False
    for v in (pat, pbt):
        if v is not None and abs(v) > rev:
            return False
    for k in ("previous_quarter", "same_quarter_last_year"):
        base = m["revenue"].get(k)
        if base is None or base <= 0 or not (0.2 <= rev / base <= 5):
            return False
    return True


# ---------- LLM extraction ----------

class MetricRow(BaseModel):
    metric: Literal["revenue", "pbt", "pat", "finance_costs", "depreciation", "other_income", "ebitda"]
    current_quarter: Optional[float]
    previous_quarter: Optional[float]
    same_quarter_last_year: Optional[float]
    unit: str
    basis: Literal["consolidated", "standalone"]
    page: int
    quote: str


class ResultsExtraction(BaseModel):
    rows: list[MetricRow]


PROMPT = ("Extract quarterly figures from this Indian listed-company results statement. For each metric "
          "(revenue = Revenue from operations, pbt = Profit before tax, pat = Profit for the period / net profit, "
          "finance_costs, depreciation, other_income, and ebitda only if EBITDA is explicitly reported) return "
          "current_quarter, previous_quarter (immediately preceding quarter) and same_quarter_last_year exactly as "
          "printed (negative for figures in brackets). unit = the stated unit (crore, lakh, million). "
          "basis = consolidated or standalone. page = the page number shown in the [PAGE n] marker. "
          "quote = the row label and numbers copied verbatim from that page. Prefer the consolidated table; "
          "if only standalone exists, use it. Omit any metric that is not present.")


def build(metrics: dict, unit: str, basis: str, source: str, cross: Optional[dict] = None) -> dict:
    """metrics: name -> {current_quarter, previous_quarter, same_quarter_last_year, page, quote} in `unit`."""
    conf, notes = "high", []
    norm = {}
    for m, v in metrics.items():
        norm[m] = {k: to_crore(v.get(k), unit) for k in ("current_quarter", "previous_quarter", "same_quarter_last_year")}
        norm[m].update(page=v.get("page"), quote=v.get("quote"))
    if "ebitda" not in norm and all(k in norm for k in ("pbt", "finance_costs", "depreciation")):
        e = {k: ebitda_from(norm["pbt"][k], norm["finance_costs"][k], norm["depreciation"][k],
                            norm.get("other_income", {}).get(k)) for k in ("current_quarter", "previous_quarter", "same_quarter_last_year")}
        e.update(page=norm["pbt"]["page"], quote=None, derived="PBT + finance costs + depreciation - other income")
        norm["ebitda"] = e
    table = []
    for m in CORE:
        v = norm.get(m)
        if not v:
            table.append({"metric": m, "current": None, "qoq": None, "yoy": None, "found": False})
            continue
        qoq = pct_change(v["current_quarter"], v["previous_quarter"])
        yoy = pct_change(v["current_quarter"], v["same_quarter_last_year"])
        for ch in (qoq, yoy):
            if ch is not None and abs(ch) > 300:
                conf = "low"; notes.append(f"{m} change beyond ±300%")
        table.append({"metric": m, "current": v["current_quarter"], "qoq": qoq, "yoy": yoy, "found": True,
                      "page": v.get("page"), "quote": v.get("quote"), "derived": v.get("derived")})
    rev = norm.get("revenue", {}).get("current_quarter")
    if rev is not None and not (1 <= abs(rev) <= 400000):
        conf = "low"; notes.append("revenue magnitude looks wrong for ₹ crore")
    if not norm.get("revenue") or not norm.get("pat"):
        conf = "low"; notes.append("core metric missing")
    elif not sane(norm):
        conf = "low"; notes.append("figures fail P&L sanity check")
    if cross:
        for m in ("revenue", "pat"):
            a, b = norm.get(m, {}).get("current_quarter"), cross.get(m, {}).get("current_quarter")
            if a is not None and b is not None and abs(a - b) > max(0.01 * abs(b), 0.05):
                conf = "low"; notes.append(f"{m}: LLM and parser disagree")
    return {"basis": basis, "unit_reported": unit, "unit": "₹ crore", "confidence": conf, "notes": notes,
            "source": source, "table": table}


def extract(pages: list[str]) -> Optional[dict]:
    cands = candidate_pages(pages)
    if not cands:
        return None
    # deterministic pass: first candidate page whose parse passes the sanity checks
    det, det_unit, det_basis = {}, None, None
    for p in cands[:6]:
        parsed = parse_page(pages[p - 1])
        unit = detect_unit(pages[p - 1])
        if parsed.get("revenue") and parsed.get("pat") and unit and sane(parsed):
            det = {k: dict(v, page=p) for k, v in parsed.items()}
            det_unit, det_basis = unit, page_basis(pages[p - 1]) or "standalone"
            break
    det_norm = None
    if det:
        try:
            det_norm = {m: {"current_quarter": to_crore(v["current_quarter"], det_unit)} for m, v in det.items()}
        except ValueError:
            det_norm = None

    if llm_mod.available():
        try:
            data = "\n\n".join(f"[PAGE {p}]\n{pages[p - 1][:9000]}" for p in cands[:3])
            res = llm_mod.llm("results_extract", PROMPT, ResultsExtraction, "cheap", data)
            rows = grounding.verify([r.model_dump() for r in res.rows], pages)
            if rows:
                basis = "consolidated" if any(r["basis"] == "consolidated" for r in rows) else "standalone"
                rows = [r for r in rows if r["basis"] == basis]
                unit = rows[0]["unit"]
                metrics = {r["metric"]: r for r in rows}
                if "revenue" in metrics and "pat" in metrics:
                    return build(metrics, unit, basis, "llm", cross=det_norm if det_basis == basis else None)
        except Exception as e:
            log.warning("LLM results extraction failed, falling back to parser: %s", e)
    if det:
        return build(det, det_unit, det_basis, "parser")
    return None
