"""Push pipeline: classify -> enrich -> score -> one-liner. Processes only unprocessed announcements.

Run: python -m finn.push
"""
import json, logging, os, re
from datetime import datetime, timedelta
from typing import Literal, Optional
import requests
from pydantic import BaseModel
from . import db, grounding, llm as llm_mod, pdfs, results

log = logging.getLogger("finn.push")

TYPE_WEIGHTS = {
    "results": 1.0, "m_and_a": 0.95, "mgmt_change": 0.9, "fundraise": 0.8, "credit_rating": 0.75,
    "order_win": 0.7, "litigation_regulatory": 0.7, "insider_pledge": 0.6, "dividend_action": 0.4,
    "other": 0.3, "investor_meet": 0.25, "board_meeting_notice": 0.15, "routine_compliance": 0.05,
}
SENIOR_WEIGHT = 1.0  # mgmt_change involving CEO / CFO / MD
BUCKETS = [("attention", 0.7), ("worth_a_look", 0.3)]
TYPES = list(TYPE_WEIGHTS)
SENIOR_RE = re.compile(r"\b(ceo|cfo|chief executive officer|chief financial officer|managing director|md)\b", re.I)

# Ordered rules: first match wins. Matched against "category | subcategory | subject" (lower-case).
RULES = [
    ("board_meeting_notice", [r"^board meeting \| board meeting", r"intimation of board meeting", r"\bto consider\b",
                              r"to announce .*results on", r"prior intimation"]),
    ("routine_compliance", [r"newspaper", r"trading window", r"reg(ulation|\.)\s*74", r"share certificate", r"esop|esos|esps|stock option",
                            r"postal ballot", r"\bagm\b|\begm\b", r"compliance certificate", r"statutory auditor", r"registrar|\brta\b",
                            r"commercial paper", r"half yearly report", r"financial statements", r"udin", r"loss of|duplicate",
                            r"shareholding pattern", r"secretarial", r"investor complaints", r"book closure", r"certificate pursuant",
                            r"allotment of equity shares", r"special window", r"esg rating"]),
    ("results", [r"^result \|", r"financial results", r"\bresults? for the quarter"]),
    ("mgmt_change", [r"change in management", r"change in directorate", r"resignation", r"cessation", r"retirement",
                     r"appointment of", r"key managerial", r"re-?appointment"]),
    ("m_and_a", [r"acquisition", r"amalgamation", r"merger", r"scheme of arrangement", r"business transfer", r"share purchase agreement",
                 r"demerger", r"divest", r"\bsale of\b", r"joint venture"]),
    ("fundraise", [r"issue of securities", r"\bqip\b", r"preferential", r"rights issue", r"\bncds?\b", r"debentures?", r"fund ?rais",
                   r"initial public offer|\bipo\b", r"listing of equity shares"]),
    ("order_win", [r"(bagging|receiving|award|receipt) of order", r"order\(s\) or contract", r"\border win", r"bags? .*order"]),
    ("credit_rating", [r"credit rating", r"\brating\b"]),
    ("dividend_action", [r"dividend", r"buy-?back", r"\bbonus\b", r"stock split|sub-division", r"record date"]),
    ("litigation_regulatory", [r"litigation", r"penalt", r"show cause", r"sebi order", r"tax demand", r"\bgst\b", r"income tax", r"rumou?r",
                               r"clarification", r"insolvency|cirp", r"regulatory action", r"\bfine\b", r"dispute", r"labor certification"]),
    ("insider_pledge", [r"pledge", r"\bsast\b", r"insider trading", r"encumbrance"]),
    ("investor_meet", [r"analyst", r"investor meet", r"investor conference", r"earnings call", r"conference call", r"audio recording",
                       r"investor presentation", r"earnings presentation", r"analyst presentation", r"transcript", r"meeting updates",
                       r"investor call", r"media statement"]),
]


def rule_classify(category: str, subcategory: str, subject: str) -> Optional[str]:
    text = f"{category} | {subcategory} | {subject}".lower()
    for t, pats in RULES:
        if any(re.search(p, text) for p in pats):
            return t
    return None


def bucket_for(score: float) -> str:
    for name, th in BUCKETS:
        if score >= th:
            return name
    return "routine"


def score_for(ann_type: str, magnitude: float, novelty: float, senior: bool = False) -> float:
    w = SENIOR_WEIGHT if (ann_type == "mgmt_change" and senior) else TYPE_WEIGHTS.get(ann_type, 0.3)
    return round(w * magnitude * novelty, 3)


# ---------- LLM schemas ----------

class Person(BaseModel):
    name: str
    role: str
    direction: Literal["appointed", "resigned", "retired", "redesignated", "other"]
    effective_date: Optional[str]
    profile: Optional[str]
    quote: str
    page: int


class MgmtExtraction(BaseModel):
    people: list[Person]


class Bio(BaseModel):
    bio: str
    url: Optional[str]


def _ann_text(a) -> str:
    return f"Category: {a['category']}\nSubcategory: {a['subcategory']}\nSubject: {a['subject']}\nSummary: {a['more'] or a['headline']}"


class TypedItem(BaseModel):
    id: int
    type: Literal["results", "mgmt_change", "m_and_a", "fundraise", "order_win", "credit_rating", "dividend_action",
                  "litigation_regulatory", "insider_pledge", "board_meeting_notice", "investor_meet",
                  "routine_compliance", "other"]


class TypedBatch(BaseModel):
    items: list[TypedItem]


class LineItem(BaseModel):
    id: int
    line: str


class LineBatch(BaseModel):
    items: list[LineItem]


CLASSIFY_PROMPT = ("Classify each Indian stock-exchange filing below into exactly one type. Product launches, "
                   "partnerships, awards and generic press releases are 'other' unless they clearly fit a listed type. "
                   "Purely procedural items are 'routine_compliance'. Return one entry per id.")
ONELINER_PROMPT = ("For each filing below write ONE line of at most 20 words for an investor who holds the stock: what "
                   "happened and why it matters. Facts from the filing only, no advice, no hype. Return one entry per id.")
BATCH = 40


def classify_batch(items: list[dict]) -> dict[int, tuple[str, str]]:
    """Rules first; only unmatched items go to the CHEAP model, batched into as few calls as possible."""
    out, unmatched = {}, []
    for a in items:
        t = rule_classify(a["category"], a["subcategory"], a["subject"])
        if t:
            out[a["id"]] = (t, "rules")
        else:
            unmatched.append(a)
    for i in range(0, len(unmatched), BATCH):
        chunk = unmatched[i:i + BATCH]
        got = {}
        if llm_mod.available():
            try:
                data = "\n\n".join(f"[ID {a['id']}]\n{_ann_text(a)[:600]}" for a in chunk)
                r = llm_mod.llm("classify", CLASSIFY_PROMPT, TypedBatch, "cheap", data)
                got = {x.id: x.type for x in r.items}
            except Exception as e:
                log.warning("classify batch failed: %s", e)
        for a in chunk:
            out[a["id"]] = (got[a["id"]], "llm") if a["id"] in got else ("other", "fallback")
    return out


def _fallback_line(a) -> dict:
    raw = (a["more"] or a["headline"] or a["subject"] or "").strip()
    words = raw.split()
    return {"text": " ".join(words[:20]) + ("…" if len(words) > 20 else ""), "source": "filing"}


def one_liners(items: list[dict]) -> dict[int, dict]:
    out = {}
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        got = {}
        if llm_mod.available():
            try:
                data = "\n\n".join(f"[ID {a['id']}] Company: {a['symbol']}\n{_ann_text(a)[:900]}" for a in chunk)
                r = llm_mod.llm("one_liner", ONELINER_PROMPT, LineBatch, "cheap", data)
                got = {x.id: x.line for x in r.items}
            except Exception as e:
                log.warning("one-liner batch failed: %s", e)
        for a in chunk:
            out[a["id"]] = ({"text": " ".join(got[a["id"]].split()[:20]), "source": "llm"} if got.get(a["id"])
                            else _fallback_line(a))
    return out


def _pdf_pages(a, plumber=False):
    if not a["pdf_url"]:
        return []
    path = pdfs.download(a["pdf_url"], symbol=a["symbol"], kind="filing", title=a["subject"])
    return pdfs.pages_pdfplumber(path) if plumber else pdfs.pages_pymupdf(path, max_pages=30)


def enrich_results(a) -> dict:
    pages = _pdf_pages(a, plumber=True)
    if not pages or not any(p.strip() for p in pages):
        return {"kind": "results", "ok": False, "reason": "PDF has no text layer (scanned)"}
    r = results.extract(pages)
    if not r:
        return {"kind": "results", "ok": False, "reason": "Results table not found in filing"}
    return {"kind": "results", "ok": True, **r}


def _tavily(query: str) -> Optional[dict]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return None
    try:
        r = requests.post("https://api.tavily.com/search", json={"api_key": key, "query": query, "max_results": 3}, timeout=20)
        r.raise_for_status()
        res = r.json().get("results") or []
        return res[0] if res else None
    except Exception as e:
        log.warning("tavily failed: %s", e)
        return None


_PERSON_RE = re.compile(r"\b(?:Mr|Ms|Mrs|Dr|Shri|Smt)\.?\s+([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})")


def _mgmt_fallback(pages, a):
    """No LLM: regex the first named person from the filing; flagged low confidence."""
    text = "\n".join(pages[:3]) or (a["more"] or "")
    m = _PERSON_RE.search(text)
    if not m:
        return []
    window = text[max(0, m.start() - 300): m.end() + 300]
    direction = "resigned" if re.search(r"resign|cessation|retire|step(ping)? down", window, re.I) else "appointed"
    role = SENIOR_RE.search(window)
    return [{"name": m.group(1).strip(), "role": role.group(0) if role else "Director / KMP", "direction": direction,
             "effective_date": None, "profile": None, "quote": text[m.start(): m.end() + 80].strip(), "page": 1,
             "confidence": "low"}]


def enrich_mgmt(a) -> dict:
    pages = _pdf_pages(a)
    people = []
    if pages and llm_mod.available():
        try:
            data = "\n\n".join(f"[PAGE {i}]\n{t[:6000]}" for i, t in enumerate(pages[:6], 1))
            r = llm_mod.llm("mgmt_extract", "Extract each person whose appointment, resignation, retirement or "
                            "re-designation is disclosed in this SEBI Reg 30 filing: name, role, direction, effective date, "
                            "and the brief profile if the filing includes one (max 40 words, from the filing only). "
                            "quote = a verbatim sentence naming the person; page = its [PAGE n] number.",
                            MgmtExtraction, "cheap", data)
            people = grounding.verify([p.model_dump() for p in r.people], pages)
        except Exception as e:
            log.warning("mgmt LLM failed %s: %s", a["id"], e)
    if not people and pages:
        people = grounding.verify(_mgmt_fallback(pages, a), pages)
    for p in people:
        src = _tavily(f"{p['name']} {a['symbol']} {p['role']}")
        if src and llm_mod.available():
            try:
                b = llm_mod.llm("mgmt_bio", "Write a factual bio of at most 40 words for this person using only the "
                                "search result below. Set url to the result URL.", Bio, "cheap",
                                json.dumps({"person": p["name"], "role": p["role"], "result": src})[:6000])
                p["bio"], p["bio_url"] = " ".join(b.bio.split()[:40]), src.get("url")
            except Exception as e:
                log.warning("bio failed: %s", e)
        if not p.get("bio") and p.get("profile"):
            p["bio"], p["bio_url"] = " ".join(p["profile"].split()[:40]), a["pdf_url"]
    if people:
        senior = any(SENIOR_RE.search(p.get("role") or "") for p in people)
    else:
        senior = bool(SENIOR_RE.search(a["subject"] + " " + (a["more"] or "")))
    return {"kind": "mgmt_change", "ok": bool(people), "people": people, "senior": senior}


def results_magnitude(enr: dict) -> float:
    if not enr.get("ok"):
        return 0.9
    pat = next((t for t in enr["table"] if t["metric"] == "pat"), None)
    yoy = abs(pat["yoy"]) if pat and pat.get("yoy") is not None else 0
    return round(0.8 + 0.2 * min(1.0, yoy / 30.0), 3)


def novelty_for(a, ann_type) -> float:
    """One event often arrives as several filings (e.g. 'Result' + 'Outcome of board meeting'). Only the primary
    filing keeps novelty 1.0: for results the one in the 'Result' category, otherwise the earliest."""
    t = datetime.fromisoformat(a["filed_at"])
    lo, hi = (t - timedelta(days=3)).isoformat(), (t + timedelta(days=3)).isoformat()
    group = db.q("SELECT id, category, filed_at FROM announcements WHERE symbol=? AND ann_type=? AND filed_at BETWEEN ? AND ?",
                 (a["symbol"], ann_type, lo, hi))
    if len(group) <= 1:
        return 1.0
    key = (lambda o: (o["category"] != "Result", o["filed_at"], o["id"])) if ann_type == "results" else (lambda o: (o["filed_at"], o["id"]))
    primary = min(group, key=key)
    if primary["id"] == a["id"]:
        return 1.0
    return 0.25 if ann_type == "results" else 0.5


def process(a, ann_type, src) -> bool:
    """Enrich + score one item. Returns True when it needs a one-liner."""
    novelty = novelty_for(a, ann_type)
    enr, magnitude, senior = None, 1.0, False
    if ann_type == "results" and novelty == 1.0:
        enr = enrich_results(a)
        magnitude = results_magnitude(enr)
    elif ann_type == "mgmt_change":
        enr = enrich_mgmt(a)
        senior = enr["senior"]
        magnitude = 1.0 if senior else 0.85
    score = score_for(ann_type, magnitude, novelty, senior)
    bucket = bucket_for(score)
    db.x("UPDATE announcements SET processed=1, magnitude=?, novelty=?, score=?, bucket=?, enrichment_json=?, "
         "status='ok', error=NULL WHERE id=?",
         (magnitude, novelty, score, bucket, json.dumps(enr) if enr else None, a["id"]))
    return bucket == "worth_a_look" or (bucket == "attention" and not (enr and enr.get("ok")))


def run(limit: Optional[int] = None) -> dict:
    todo = db.q("SELECT * FROM announcements WHERE processed=0 ORDER BY filed_at" + (f" LIMIT {int(limit)}" if limit else ""))
    types = classify_batch(todo)
    for a in todo:  # store every type first so novelty can see the whole group of related filings
        t, src = types.get(a["id"], ("other", "fallback"))
        db.x("UPDATE announcements SET ann_type=?, type_source=? WHERE id=?", (t, src, a["id"]))
    ok = failed = 0
    need_line = []
    for a in todo:
        try:
            t, src = types.get(a["id"], ("other", "fallback"))
            if process(a, t, src):
                need_line.append(a)
            ok += 1
        except Exception as e:
            log.exception("push failed for announcement %s", a["id"])
            db.x("UPDATE announcements SET processed=1, status='error', error=?, score=COALESCE(score,0.3), "
                 "bucket=COALESCE(bucket,'worth_a_look') WHERE id=?", (f"couldn't process: {e}"[:500], a["id"]))
            failed += 1
    try:
        for aid, line in one_liners(need_line).items():
            db.x("UPDATE announcements SET one_liner=? WHERE id=?", (json.dumps(line), aid))
    except Exception as e:
        log.warning("one-liners failed: %s", e)
    return {"processed": ok, "failed": failed, "one_liners": len(need_line)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    print(run())
