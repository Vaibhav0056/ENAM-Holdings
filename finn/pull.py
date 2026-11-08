"""Pull research: Promised vs Delivered, grounded only in concall transcripts + the annual report.

Stages: Finding documents -> Reading transcripts -> Extracting guidance -> Matching to results ->
Writing verdict -> Verifying citations.
"""
import json, logging, re, threading, time, uuid
from datetime import datetime, timedelta
from typing import Literal, Optional
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel
from rapidfuzz import fuzz
from . import db, grounding, llm as llm_mod, pdfs

log = logging.getLogger("finn.pull")
UA = {"User-Agent": "Mozilla/5.0 (compatible; FINN-research-app/0.1)"}
STAGES = ["Finding documents", "Reading transcripts", "Extracting guidance", "Matching to results",
          "Writing verdict", "Verifying citations"]
REPORT_TTL = timedelta(hours=24)
MAX_TRANSCRIPTS = 4


class NoSuchTicker(ValueError):
    pass


# ---------------- documents ----------------

def _bse_pdf(href: str) -> str:
    m = re.search(r"Pname=([\w\-]+\.pdf)", href or "", re.I)
    return f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{m.group(1)}" if m else href


def find_documents(symbol: str) -> dict:
    html, used = None, None
    for url in (f"https://www.screener.in/company/{symbol}/consolidated/", f"https://www.screener.in/company/{symbol}/"):
        r = requests.get(url, headers=UA, timeout=30)
        time.sleep(1)
        if r.ok and "section" in r.text and 'id="documents"' in r.text:
            html, used = r.text, url
            break
    if not html:
        raise NoSuchTicker(f"{symbol} not found on screener.in")
    soup = BeautifulSoup(html, "html.parser")
    name = (soup.select_one("h1") or soup.new_tag("x")).get_text(strip=True) or symbol
    transcripts, seen = [], set()
    for li in soup.select("section#documents .concalls li"):
        a = next((a for a in li.select("a") if a.get_text(strip=True).lower().startswith("transcript")), None)
        if not a or not a.get("href"):
            continue
        href = _bse_pdf(a["href"])
        if href in seen:
            continue
        seen.add(href)
        label = " ".join(li.get_text(" ", strip=True).split()[:2])  # e.g. "Oct 2026"
        transcripts.append({"kind": "transcript", "label": label, "url": href})
        if len(transcripts) >= MAX_TRANSCRIPTS:
            break
    ar = None
    for a in soup.select("section#documents .annual-reports li a"):
        if a.get("href", "").lower().endswith(".pdf") or "bseindia" in a.get("href", ""):
            ar = {"kind": "annual_report", "label": a.get_text(" ", strip=True)[:40], "url": _bse_pdf(a["href"])}
            break
    return {"name": name, "screener_url": used, "transcripts": transcripts, "annual_report": ar}


# ---------------- transcript parsing ----------------

_TURN_RE = re.compile(r"^\s*([A-Z][A-Za-z.'\- ]{1,40}?)\s*:\s*(.*)$")
_NOT_SPEAKERS = {"tel", "date", "email", "web", "cin", "isin", "subject", "address", "registered office",
                 "corporate office", "scrip code", "note", "website", "fax", "phone", "dn", "membership no."}


def management_names(pages: list[str]) -> set[str]:
    """Names listed in the transcript's MANAGEMENT block (cover pages)."""
    head = "\n".join(pages[:3])
    m = re.search(r"MANAGEMENT\s*:?(.{0,2500}?)(?:MODERATOR|Moderator:|Ladies and gentlemen)", head, re.S | re.I)
    block = m.group(1) if m else ""
    names = set()
    for n in re.findall(r"\b(?:MR|MS|MRS|DR|SHRI)\.?\s+([A-Z][A-Za-z.\s]{2,40}?)\s*[–\-,]", block, re.I):
        names.add(" ".join(n.split()).lower())
    return names


def split_turns(pages: list[str]) -> list[dict]:
    mgmt = management_names(pages)
    turns, cur = [], None
    for pno, text in enumerate(pages, 1):
        for ln in text.splitlines():
            m = _TURN_RE.match(ln)
            if m and m.group(1).strip().lower() not in _NOT_SPEAKERS and len(m.group(1).split()) <= 5:
                if cur:
                    turns.append(cur)
                cur = {"speaker": m.group(1).strip(), "page": pno, "text": m.group(2).strip()}
            elif cur:
                cur["text"] += " " + ln.strip()
    if cur:
        turns.append(cur)
    first_q = next((i for i, t in enumerate(turns) if t["speaker"].lower() == "moderator" and
                    re.search(r"first question|question-and-answer|question and answer", t["text"], re.I)), len(turns))
    opening = {t["speaker"].lower() for t in turns[:first_q] if t["speaker"].lower() != "moderator"}
    for t in turns:
        s = t["speaker"].lower()
        if s == "moderator":
            t["role"] = "moderator"
        elif any(fuzz.token_set_ratio(s, n) >= 85 for n in mgmt) or (not mgmt and s in opening) or (s in opening and len(opening) <= 6):
            t["role"] = "management"
        else:
            t["role"] = "analyst"
        t["text"] = " ".join(t["text"].split())
    return turns


# ---------------- LLM schemas ----------------

class GuidanceItem(BaseModel):
    quarter_said: str
    speaker: str
    metric: str
    target_text: str
    target_low: Optional[float]
    target_high: Optional[float]
    unit: str
    horizon: str
    quote: str
    page: int


class ReportedActual(BaseModel):
    period: str
    metric: str
    value: float
    unit: str
    quote: str
    page: int


class TranscriptExtraction(BaseModel):
    quarter: str
    guidance: list[GuidanceItem]
    actuals: list[ReportedActual]


class Cited(BaseModel):
    text: str
    quote: str
    page: int


class Segment(BaseModel):
    name: str
    description: str
    share_pct: Optional[float]
    quote: str
    page: int


class Snapshot(BaseModel):
    what_it_does: Optional[Cited]
    segments: list[Segment]
    revenue_mix: list[Cited]
    actuals: list[ReportedActual]


class Bullet(BaseModel):
    text: str
    evidence: list[str]


class Synthesis(BaseModel):
    credibility: list[Bullet]
    bull: list[Bullet]
    bear: list[Bullet]


TRANSCRIPT_PROMPT = (
    "These are MANAGEMENT remarks from one Indian listed company's earnings call, marked with [PAGE n] and speaker. "
    "1) quarter: the quarter this call discusses, as 'Qn FYyy' (Indian fiscal year, e.g. Q2 FY27 = Jul-Sep 2026). "
    "2) guidance: every forward-looking target management gives (revenue growth, margins, capex, volumes, order book, "
    "debt, store/plant additions, etc.). target_low/target_high are numbers from the text (same value twice for a point "
    "target, null if the target is qualitative like 'healthy growth'). unit like '%', 'crore', 'bps', 'x', 'stores'. "
    "horizon: the period the target applies to as 'FYyy', 'Qn FYyy', 'H1 FYyy' or 'FYyy-FYyy'; if they say 'this year' "
    "use the current fiscal year; 'next year' the following one; 'medium term' -> 'medium term'. "
    "3) actuals: figures management reports as already achieved for a completed period (e.g. 'revenue grew 47% YoY in the "
    "quarter', 'EBITDA margin was 14.4%'), period as 'Qn FYyy', 'H1 FYyy' or 'FYyy'. Use the same metric wording style "
    "as guidance (e.g. 'revenue growth', 'EBITDA margin'). "
    "quote must be copied verbatim from the text (one sentence, max 40 words); page = its [PAGE n].")

AR_PROMPT = (
    "This is an excerpt of an Indian listed company's annual report with [PAGE n] markers. Extract: what_it_does (one "
    "sentence describing the business), segments (business segments with short description and share of revenue if "
    "stated), revenue_mix (geography/product mix statements with numbers), and actuals: reported full-year figures "
    "(revenue growth %, EBITDA margin %, PAT, capex, etc.) with period as 'FYyy'. Every item needs a verbatim quote "
    "(max 40 words) and its page number. Only use facts present in the text.")

SYNTH_PROMPT = (
    "You are given ONLY verified evidence (JSON) about a listed company: management guidance with verdicts, reported "
    "actuals and a business snapshot. Each item has an id. Write: credibility (at most 5 bullets) assessing how reliably "
    "management delivers on what it says; bull (exactly 3 bullets) and bear (exactly 3 bullets). Each bullet max 25 "
    "words, must cite ids in evidence, and may only use numbers that appear in the evidence. No buy/sell advice.")


# ---------------- periods & matching (pure python, tested) ----------------

def parse_period(s: str) -> Optional[tuple[int, int]]:
    """'Q2 FY27' -> (27, 2); 'FY27' -> (27, 4); 'H1 FY27' -> (27, 2); 'FY26-FY28'/'FY28' end -> (28, 4)."""
    if not s:
        return None
    t = s.upper().replace("'", " ").replace("FISCAL", "FY")
    fys = re.findall(r"FY\s*(\d{2,4})", t)
    if not fys:
        m = re.search(r"\b(20\d{2})\b", t)
        if not m:
            return None
        fys = [m.group(1)]
    fy = int(fys[-1]) % 100
    q = re.search(r"\bQ([1-4])\b", t)
    if q and len(fys) == 1:
        return (fy, int(q.group(1)))
    h = re.search(r"\bH([12])\b", t)
    if h and len(fys) == 1:
        return (fy, 2 if h.group(1) == "1" else 4)
    return (fy, 4)


_GENERIC = {"growth", "rate", "increase", "level", "share", "contribution", "guidance", "target", "ratio",
            "percentage", "cycle", "day", "steady", "state", "long", "term", "medium", "annual", "run", "net", "gross", "from"}


def _metric_tokens(m: str) -> list[str]:
    m = m.lower().replace("capital expenditure", "capex").replace("&", " and ").replace("-", " ").replace("/", " ")
    m = re.sub(r"\b(the|of|our|overall|total|consolidated|company|business|yoy|y o y|year on year|in|for|to|a)\b", " ", m)
    toks = []
    for t in re.findall(r"[a-z0-9]+", m):
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # margins -> margin, wires -> wire
        toks.append(t)
    return toks


def metric_match(a: str, b: str) -> bool:
    """Same metric only: fuzzy-equal AND no segment-specific word on one side only
    ('FMEG EBITDA margin' must not match company-wide 'EBITDA margin')."""
    ta, tb = _metric_tokens(a), _metric_tokens(b)
    if not ta or not tb:
        return False
    diff = (set(ta) ^ set(tb)) - _GENERIC - {"and"}
    if diff:
        return False
    return fuzz.token_sort_ratio(" ".join(ta), " ".join(tb)) >= 70 or set(ta) - _GENERIC == set(tb) - _GENERIC


def _unit_key(u: str) -> str:
    u = (u or "").lower().strip()
    if "%" in u or "percent" in u:
        return "%"
    if "bps" in u or "basis" in u:
        return "bps"
    if "crore" in u or u in {"cr", "rs cr", "inr cr"}:
        return "crore"
    return u


def verdict_for(g: dict, actual: Optional[dict], latest: Optional[tuple]) -> str:
    lo, hi = g.get("target_low"), g.get("target_high")
    if lo is None and hi is None:
        return "unverifiable"
    hz = parse_period(g.get("horizon", ""))
    if actual is None:
        if hz is None or (latest and hz > latest) or not latest:
            return "pending"
        return "unverifiable"
    if _unit_key(actual["unit"]) != _unit_key(g["unit"]):
        return "unverifiable"
    v = actual["value"]
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    lower_is_better = bool(re.search(r"debt|cost|working capital|days|leverage|attrition", g["metric"], re.I))
    if lower_is_better:
        if v <= hi:
            return "delivered"
        return "partially" if v <= hi * 1.1 else "missed"
    if v >= lo:
        return "delivered"
    return "partially" if v >= lo * 0.9 else "missed"


def match(guidance: list[dict], actuals: list[dict]) -> list[dict]:
    periods = [parse_period(a["period"]) for a in actuals] + [parse_period(g["quarter_said"]) for g in guidance]
    latest = max([p for p in periods if p], default=None)
    rows = []
    for g in guidance:
        hz = parse_period(g.get("horizon", ""))
        cands = [a for a in actuals if metric_match(g["metric"], a["metric"])]
        exact = [a for a in cands if hz and parse_period(a["period"]) == hz]
        actual = exact[0] if exact else None
        progress = None
        if not actual:
            # latest reported figure for the same metric, before the horizon ends (or any, for open horizons)
            dated = [a for a in cands if parse_period(a["period"]) and (hz is None or parse_period(a["period"]) < hz)]
            progress = max(dated, key=lambda a: parse_period(a["period"])) if dated else None
        verdict = verdict_for(g, actual, latest)
        tracking = None
        ratio_like = _unit_key(g.get("unit", "")) in {"%", "bps", "x"} or "day" in (g.get("unit") or "").lower()
        if progress and verdict == "pending" and ratio_like:  # a quarter's capex can't be compared to a 5-year total
            t = verdict_for(dict(g, horizon=progress["period"]), progress, latest)
            tracking = {"delivered": "on track", "partially": "slightly behind", "missed": "behind"}.get(t)
        rows.append({"guidance": g, "actual": actual, "progress": progress, "verdict": verdict, "tracking": tracking})
    return rows


# ---------------- verification of prose ----------------

_NUM_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


def _nums(s: str) -> set[str]:
    out = set()
    for n in _NUM_RE.findall(s):
        n = n.replace(",", "")
        try:
            f = float(n)
        except ValueError:
            continue
        out.add(("%g" % f))
    return out


def scrub_numbers(text: str, allowed: set[str]) -> str:
    """Drop any sentence that contains a number not present in the evidence JSON."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    keep = [s for s in sentences if _nums(re.sub(r"\b[GAS]\d+\b", "", s)) <= allowed]
    return " ".join(keep).strip()


# ---------------- job runner ----------------

def _set(job_id, stage=None, progress=None, status=None, result=None, error=None):
    sets, vals = ["updated_at=CURRENT_TIMESTAMP"], []
    for col, v in (("stage", stage), ("progress", progress), ("status", status), ("error", error)):
        if v is not None:
            sets.append(f"{col}=?"); vals.append(v)
    if result is not None:
        sets.append("result_json=?"); vals.append(json.dumps(result))
    db.x(f"UPDATE research_jobs SET {', '.join(sets)} WHERE id=?", (*vals, job_id))


def _stage(job_id, i, frac=0.0):
    _set(job_id, stage=STAGES[i], progress=round((i + frac) / len(STAGES), 3))


def _load_doc(d, symbol):
    path = pdfs.download(d["url"], symbol=symbol, kind=d["kind"], title=d["label"], polite_sleep=1.0)
    return pdfs.pages_pymupdf(path, max_pages=450)


def _ar_pages_for(pages: list[str]) -> list[int]:
    kw = ["segment", "revenue", "business", "we are", "our company", "ebitda", "margin", "growth", "highlights",
          "key performance", "financial performance", "overview", "capex", "products", "customers"]
    scored = []
    for i, t in enumerate(pages[:220], 1):
        low = t.lower()
        if len(low) < 400:
            continue
        s = sum(low.count(k) for k in kw)
        if "notes to" in low or "independent auditor" in low or "form no. mgt" in low:
            s *= 0.2
        scored.append((s, i))
    return sorted(i for _, i in sorted(scored, reverse=True)[:10])


def _ingest_transcripts(job_id, docs, symbol):
    out = []
    for k, d in enumerate(docs["transcripts"]):
        try:
            pages = _load_doc(d, symbol)
            turns = split_turns(pages)
            mg = [t for t in turns if t["role"] == "management"]
            if not mg:
                raise ValueError("no management turns found (scanned or unusual format)")
            out.append({**d, "pages": pages, "turns": mg, "ok": True})
        except Exception as e:
            log.warning("transcript failed %s: %s", d["url"], e)
            out.append({**d, "ok": False, "error": str(e)})
        _stage(job_id, 1, (k + 1) / max(1, len(docs["transcripts"])))
    return out


def run_research(symbol: str, job_id: str):
    symbol = symbol.upper().strip()
    _stage(job_id, 0)
    docs = find_documents(symbol)
    notes = []
    if not docs["transcripts"]:
        notes.append("No concall transcripts found on screener.in; snapshot built from the annual report only.")

    _stage(job_id, 1)
    transcripts = _ingest_transcripts(job_id, docs, symbol)
    ar = None
    if docs["annual_report"]:
        try:
            ar = {**docs["annual_report"], "pages": _load_doc(docs["annual_report"], symbol), "ok": True}
        except Exception as e:
            notes.append(f"Annual report couldn't be read: {e}")
    if not llm_mod.available():
        raise RuntimeError("GEMINI_API_KEY is required for research extraction")

    _stage(job_id, 2)
    guidance, actuals, sources = [], [], []
    for k, t in enumerate(transcripts):
        src_id = f"D{len(sources) + 1}"
        sources.append({"id": src_id, "kind": "transcript", "label": t["label"], "url": t["url"], "ok": t["ok"],
                        "error": t.get("error")})
        if not t["ok"]:
            continue
        data = "\n".join(f"[PAGE {x['page']}] [{x['speaker']}] {x['text']}" for x in t["turns"])[:120000]
        try:
            ex = llm_mod.llm("guidance_extract", TRANSCRIPT_PROMPT, TranscriptExtraction, "cheap", data)
        except Exception as e:
            sources[-1].update(ok=False, error=f"extraction failed: {e}")
            continue
        g = grounding.verify([dict(i.model_dump(), doc=src_id) for i in ex.guidance], t["pages"])
        a = grounding.verify([dict(i.model_dump(), doc=src_id) for i in ex.actuals], t["pages"])
        for i in g:
            i["quarter_said"] = i.get("quarter_said") or ex.quarter
        sources[-1].update(quarter=ex.quarter, dropped=len(ex.guidance) + len(ex.actuals) - len(g) - len(a))
        guidance += g
        actuals += a
        _stage(job_id, 2, (k + 1) / (len(transcripts) + 1))
    snapshot = {"what_it_does": None, "segments": [], "revenue_mix": []}
    if ar:
        src_id = f"D{len(sources) + 1}"
        sources.append({"id": src_id, "kind": "annual_report", "label": ar["label"], "url": ar["url"], "ok": True})
        sel = _ar_pages_for(ar["pages"])
        data = "\n\n".join(f"[PAGE {p}]\n{ar['pages'][p - 1][:7000]}" for p in sel)
        try:
            sn = llm_mod.llm("ar_snapshot", AR_PROMPT, Snapshot, "cheap", data)
            pages = ar["pages"]
            snapshot["what_it_does"] = (grounding.verify([dict(sn.what_it_does.model_dump(), doc=src_id)], pages) or [None])[0] if sn.what_it_does else None
            snapshot["segments"] = grounding.verify([dict(s.model_dump(), doc=src_id) for s in sn.segments], pages)
            snapshot["revenue_mix"] = grounding.verify([dict(s.model_dump(), doc=src_id) for s in sn.revenue_mix], pages)
            actuals += grounding.verify([dict(s.model_dump(), doc=src_id) for s in sn.actuals], pages)
        except Exception as e:
            notes.append(f"Annual report extraction failed: {e}")
    # de-duplicate guidance (same metric + horizon + same target said in several calls -> keep latest saying)
    for i, g in enumerate(guidance, 1):
        g["id"] = f"G{i}"
    for i, a in enumerate(actuals, 1):
        a["id"] = f"A{i}"

    _stage(job_id, 3)
    rows = match(guidance, actuals)
    order = {"delivered": 0, "partially": 1, "missed": 2, "pending": 3, "unverifiable": 4}
    rows.sort(key=lambda r: (order[r["verdict"]], -(parse_period(r["guidance"]["quarter_said"]) or (0, 0))[0]))

    _stage(job_id, 4)
    evidence = {
        "promises": [{"id": r["guidance"]["id"], "metric": r["guidance"]["metric"], "target": r["guidance"]["target_text"],
                      "horizon": r["guidance"]["horizon"], "said_in": r["guidance"]["quarter_said"], "verdict": r["verdict"],
                      "actual": (r["actual"] or {}).get("value"), "actual_id": (r["actual"] or {}).get("id"),
                      "progress": (r["progress"] or {}).get("value")} for r in rows],
        "actuals": [{"id": a["id"], "period": a["period"], "metric": a["metric"], "value": a["value"], "unit": a["unit"]} for a in actuals],
        "snapshot": [{"id": f"S{i}", "text": s.get("text") or f"{s.get('name')}: {s.get('description')}"}
                     for i, s in enumerate(([snapshot["what_it_does"]] if snapshot["what_it_does"] else []) + snapshot["segments"] + snapshot["revenue_mix"], 1)],
    }
    synthesis = {"credibility": [], "bull": [], "bear": []}
    if guidance or actuals or evidence["snapshot"]:
        try:
            syn = llm_mod.llm("synthesis", SYNTH_PROMPT, Synthesis, "strong", json.dumps(evidence))
            synthesis = syn.model_dump()
        except Exception as e:
            notes.append(f"Verdict synthesis failed: {e}")

    _stage(job_id, 5)
    ev_json = json.dumps(evidence)
    allowed, ids = _nums(re.sub(r"\b[GASD]\d+\b", "", ev_json)), set(re.findall(r'"id": "([GASD]\d+)"', ev_json))
    removed = 0
    for sec in synthesis:
        cleaned = []
        for b in synthesis[sec][: 5 if sec == "credibility" else 3]:
            txt = re.sub(r"\s*[\[(]\s*(?:[GASD]\d+\s*,?\s*)+[\])]", "", b["text"]).strip()
            txt = scrub_numbers(txt, allowed)
            evid = [e for e in b["evidence"] if e in ids]
            if txt and evid:
                cleaned.append({"text": txt, "evidence": evid})
            else:
                removed += 1
        synthesis[sec] = cleaned
    result = {
        "symbol": symbol, "name": docs["name"], "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": sources, "notes": notes, "rows": rows, "actuals": actuals, "snapshot": snapshot,
        "synthesis": synthesis, "removed_bullets": removed,
        "counts": {k: sum(r["verdict"] == k for r in rows) for k in order},
    }
    db.x("INSERT OR REPLACE INTO research_reports(symbol,result_json,created_at) VALUES(?,?,CURRENT_TIMESTAMP)",
         (symbol, json.dumps(result)))
    _set(job_id, stage="Done", progress=1.0, status="done", result=result)
    return result


def cached_report(symbol: str) -> Optional[dict]:
    r = db.q("SELECT * FROM research_reports WHERE symbol=?", (symbol.upper(),), one=True)
    if not r:
        return None
    created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.utcnow() - created > REPORT_TTL:
        return None
    return json.loads(r["result_json"])


def start(symbol: str, force: bool = False) -> str:
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", symbol):
        raise NoSuchTicker("invalid symbol")
    job_id = uuid.uuid4().hex[:12]
    cached = None if force else cached_report(symbol)
    if cached:
        db.x("INSERT INTO research_jobs(id,symbol,stage,progress,status,result_json) VALUES(?,?,?,?,?,?)",
             (job_id, symbol, "Done", 1.0, "done", json.dumps(cached)))
        return job_id
    running = db.q("SELECT id FROM research_jobs WHERE symbol=? AND status='running' AND updated_at>=datetime('now','-10 minutes')",
                   (symbol,), one=True)
    if running:
        return running["id"]
    db.x("INSERT INTO research_jobs(id,symbol,stage,progress,status) VALUES(?,?,?,?,?)", (job_id, symbol, STAGES[0], 0.0, "running"))

    def work():
        try:
            run_research(symbol, job_id)
        except Exception as e:
            log.exception("research failed %s", symbol)
            _set(job_id, status="error", error=str(e))
    threading.Thread(target=work, daemon=True).start()
    return job_id


def job(job_id: str) -> Optional[dict]:
    j = db.q("SELECT * FROM research_jobs WHERE id=?", (job_id,), one=True)
    if not j:
        return None
    return {"id": j["id"], "symbol": j["symbol"], "stage": j["stage"], "progress": j["progress"], "status": j["status"],
            "error": j["error"], "stages": STAGES, "result": json.loads(j["result_json"]) if j["result_json"] else None}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for sym in sys.argv[1:]:
        jid = uuid.uuid4().hex[:12]
        db.x("INSERT INTO research_jobs(id,symbol,stage,progress,status) VALUES(?,?,?,?,?)", (jid, sym.upper(), STAGES[0], 0, "running"))
        try:
            r = run_research(sym, jid)
            print(sym, r["counts"], "sources", [(s["label"], s["ok"]) for s in r["sources"]], r["notes"])
        except Exception as e:
            _set(jid, status="error", error=str(e))
            print(sym, "FAILED", e)
