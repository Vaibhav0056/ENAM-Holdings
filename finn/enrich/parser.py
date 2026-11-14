"""Deterministic results-statement parser (pure, no I/O): finds the P&L page, its unit and basis, and reads
current / previous-quarter / year-ago figures per metric from the text. Proven on the Step 1-8 PDFs; the model
reads only what this can't verify (finn/enrich/results.py)."""
import re
from typing import Optional

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
        dense = len(re.findall(r"\d[\d,]*\.\d{2}\b|\d{1,3}(?:,\d{2,3})+\b", t))
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
