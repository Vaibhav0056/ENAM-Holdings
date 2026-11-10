"""Management-change card: who, which role, in or out, effective date, the stated reason for leaving, and a profile.

The profile is the filing's own words (the "brief profile" annexure SEBI requires for appointments), quoted and
verified on its page. A Tavily search adds a cited web bio only when TAVILY_API_KEY is set (push-side enrichment is
the one place web search is allowed). Filings are batched several per model call; a regex fallback still names the
people when the model is unavailable.
"""
from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from pydantic import BaseModel

from .. import config
from ..feed.score import SENIOR_RE
from ..grounding import quote_ok

log = logging.getLogger("finn.enrich.mgmt")
BATCH = 5
MAX_PAGES = 6


class Person(BaseModel):
    filing_id: int
    name: str
    role: str
    direction: Literal["appointed", "resigned", "retired", "ceased", "redesignated", "other"]
    effective_date: Optional[str] = None
    reason_quote: Optional[str] = None
    profile_quote: Optional[str] = None
    quote: str
    page: int


class People(BaseModel):
    people: list[Person]


def _page_hit(quote: str | None, pages: list[str], page: int) -> int | None:
    if not quote:
        return None
    for cand in (page, page - 1, page + 1, *range(1, len(pages) + 1)):
        if 1 <= cand <= len(pages) and quote_ok(quote, pages[cand - 1]):
            return cand
    return None


def verify_person(p: dict, pages: list[str]) -> dict | None:
    """Keep a person only if their main quote is on the page; drop any optional quote that isn't."""
    page = _page_hit(p["quote"], pages, p["page"])
    if page is None or p["name"].split()[-1].lower() not in p["quote"].lower():
        return None
    p["page"] = page
    for k in ("reason_quote", "profile_quote"):
        if p.get(k):
            hit = _page_hit(p[k], pages, page)
            p[k + "_page"] = hit
            if hit is None:
                p[k] = None
    p["senior"] = bool(SENIOR_RE.search(p["role"] or ""))
    return p


_PERSON_RE = re.compile(r"\b(?:Mr|Ms|Mrs|Dr|Shri|Smt)\.?\s+([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})")
_DIRECTION = [("resigned", r"resign"), ("retired", r"retire"), ("ceased", r"cessation|ceas"),
              ("appointed", r"appoint")]


def fallback(subject: str, body: str, pages: list[str]) -> list[dict]:
    """No model: name the people the filing's text mentions, with the direction its wording implies."""
    text = f"{subject} {body} " + " ".join(pages[:2])[:4000]
    direction = next((d for d, pat in _DIRECTION if re.search(pat, text, re.I)), "other")
    names = list(dict.fromkeys(m.strip(" .") for m in _PERSON_RE.findall(text)))[:3]
    return [{"name": n, "role": None, "direction": direction, "quote": None, "page": None, "senior": False,
             "source": "text match"} for n in names]


def extract_batch(filings: list[dict], use_llm: bool = True) -> dict[int, list[dict]]:
    """filings: [{id, subject, body, pages}] -> {filing_id: [person, ...]} (verified people only)."""
    out: dict[int, list[dict]] = {f["id"]: [] for f in filings}
    got_any = set()
    if use_llm:
        for i in range(0, len(filings), BATCH):
            chunk = filings[i:i + BATCH]
            doc = "\n\n".join(f"[FILING {f['id']}]\nSubject: {f['subject']}\n" +
                              "\n".join(f"[PAGE {n}]\n{t[:5000]}" for n, t in enumerate(f["pages"][:MAX_PAGES], 1))
                              for f in chunk)
            try:
                from ..llm import router
                r = router.call("mgmt_extract", "mgmt_extract", {"document": doc}, People)
                by_id = {f["id"]: f for f in chunk}
                for p in r.data.people:
                    f = by_id.get(p.filing_id)
                    if not f:
                        continue
                    v = verify_person(p.model_dump(), f["pages"])
                    if v:
                        v["source"] = "filing"
                        out[f["id"]].append(v)
                got_any |= set(by_id)
            except Exception as e:  # noqa: BLE001
                log.warning("mgmt batch failed: %s", e)
    for f in filings:
        if f["id"] not in got_any and not out[f["id"]]:
            out[f["id"]] = fallback(f["subject"], f.get("body") or "", f["pages"])
    return out


def web_bio(name: str, company: str) -> dict | None:
    """Tavily, only when a key is configured. Returns {text, url} from the top result, or None."""
    if not config.TAVILY_API_KEY:
        return None
    try:
        from tavily import TavilyClient
        r = TavilyClient(api_key=config.TAVILY_API_KEY).search(f"{name} {company} professional background", max_results=3)
        top = (r.get("results") or [None])[0]
        return {"text": top["content"][:400], "url": top["url"]} if top else None
    except Exception as e:  # noqa: BLE001
        log.warning("tavily failed: %s", e)
        return None


def governance_flags(people: list[dict]) -> list[dict]:
    """Senior exits are governance-relevant on their own: a CFO or auditor leaving mid-term is the classic early
    warning. Each flag carries the verified quote it rests on."""
    flags = []
    for p in people:
        if p.get("direction") in ("resigned", "ceased") and (p.get("senior") or re.search(r"auditor|independent director",
                                                                                           p.get("role") or "", re.I)):
            flags.append({"kind": "senior_exit" if p.get("senior") else "independent_exit",
                          "severity": "high" if p.get("senior") else "medium",
                          "summary": f"{p['name']} ({p['role']}) {p['direction']}",
                          "reason_quote": p.get("reason_quote"), "quote": p.get("quote"), "page": p.get("page")})
    return flags
