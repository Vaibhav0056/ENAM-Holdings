"""Earnings-call transcripts -> speaker turns with roles (management | analyst | moderator).

Speaker check (spec §7.3): promises may only come from management. Management is the transcript's own MANAGEMENT
block on its cover pages, or, failing that, whoever speaks in the opening remarks before the moderator opens Q&A.
Ported from the Step 1-8 engine.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

_TURN_RE = re.compile(r"^\s*([A-Z][A-Za-z.'\- ]{1,40}?)\s*:\s*(.*)$")
_NOT_SPEAKERS = {"tel", "date", "email", "web", "cin", "isin", "subject", "address", "registered office",
                 "corporate office", "scrip code", "note", "website", "fax", "phone", "dn", "membership no.", "symbol",
                 "bse", "nse", "ref", "sub", "to", "dear sir", "dear sirs", "encl", "for"}
_TITLE = re.compile(r"\b(?:mr|ms|mrs|dr|shri|smt|prof)\.?\s+", re.I)


def norm_name(s: str) -> str:
    return " ".join(_TITLE.sub("", s or "").lower().replace(".", " ").split())


def management_names(pages: list[str]) -> set[str]:
    """Names in the transcript's MANAGEMENT block (cover pages)."""
    head = "\n".join(pages[:3])
    m = re.search(r"MANAGEMENT\s*(?:TEAM)?\s*:?(.{0,2500}?)(?:MODERATOR|Moderator:|Ladies and gentlemen)", head, re.S | re.I)
    block = m.group(1) if m else ""
    names = set()
    for n in re.findall(r"\b(?:MR|MS|MRS|DR|SHRI|PROF)\.?\s+([A-Z][A-Za-z.\s]{2,40}?)\s*[–\-,:(]", block, re.I):
        names.add(norm_name(n))
    return {n for n in names if n}


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
                    re.search(r"first question|question-and-answer|question and answer|q&a", t["text"], re.I)), len(turns))
    opening = {norm_name(t["speaker"]) for t in turns[:first_q] if t["speaker"].lower() != "moderator"}
    for t in turns:
        s = norm_name(t["speaker"])
        if t["speaker"].lower() == "moderator":
            t["role"] = "moderator"
        elif any(fuzz.token_set_ratio(s, n) >= 85 for n in mgmt) or (not mgmt and s in opening) or \
                (s in opening and len(opening) <= 6):
            t["role"] = "management"
        else:
            t["role"] = "analyst"
        t["text"] = " ".join(t["text"].split())
    return turns


def is_management(speaker: str, turns: list[dict]) -> bool:
    s = norm_name(speaker)
    return any(t["role"] == "management" and fuzz.token_set_ratio(s, norm_name(t["speaker"])) >= 85 for t in turns)


def management_text(turns: list[dict], limit: int = 120_000) -> str:
    """What the extractor sees: management turns only, each tagged with its page and speaker."""
    return "\n".join(f"[PAGE {t['page']}] [{t['speaker']}] {t['text']}" for t in turns if t["role"] == "management")[:limit]


def qa_pairs(turns: list[dict]) -> list[dict]:
    """Analyst question followed by the management answer(s) until the next analyst/moderator turn (deep dives)."""
    pairs, i = [], 0
    while i < len(turns):
        t = turns[i]
        if t["role"] == "analyst":
            answers, j = [], i + 1
            while j < len(turns) and turns[j]["role"] == "management":
                answers.append(turns[j])
                j += 1
            if answers:
                pairs.append({"analyst": t["speaker"], "question": t["text"], "page": t["page"],
                              "answer": " ".join(a["text"] for a in answers), "answered_by": answers[0]["speaker"],
                              "answer_page": answers[0]["page"]})
            i = j
        else:
            i += 1
    return pairs
