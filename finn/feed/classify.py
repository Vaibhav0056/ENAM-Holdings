"""Filing type: ordered rules first (BSE category | sub-category | subject), the cheap model only for what no rule
places, in batches with a constrained enum. Rules were tuned on 855 real filings (Phase 2 log)."""
from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger("finn.feed.classify")

TYPES = ["results", "mgmt_change", "governance", "m_and_a", "fundraise", "order_win", "credit_rating",
         "insider_pledge", "dividend_action", "litigation_regulatory", "investor_meet", "board_meeting_notice",
         "agm_egm", "routine_compliance", "other"]
FilingType = Literal["results", "mgmt_change", "governance", "m_and_a", "fundraise", "order_win", "credit_rating",
                     "insider_pledge", "dividend_action", "litigation_regulatory", "investor_meet",
                     "board_meeting_notice", "agm_egm", "routine_compliance", "other"]

# Ordered: first match wins. Governance red flags come before the routine list, which mentions auditors and
# pledges in their harmless forms (auditor appointed at an AGM, pledge released).
RULES: list[tuple[str, list[str]]] = [
    ("governance", [r"resignation of (the )?(statutory |secretarial |internal |joint )?auditor", r"auditors?\b.{0,40}\bresign",
                    r"related party transaction", r"forensic (audit|review)", r"special audit", r"whistle-?blower",
                    r"default (in|on) (payment|repayment)", r"\bfraud\b", r"(creation|invocation) of (pledge|encumbrance)",
                    r"resignation of (an )?independent director", r"qualified (audit )?opinion|modified opinion"]),
    # Promoter-group members moving to the public category: procedural, never a governance red flag by itself.
    ("routine_compliance", [r"reclassification (of|from|for) .{0,40}(promoter|shareholder category|public)",
                            r"reclassification from .{0,30}promoter"]),
    ("board_meeting_notice", [r"^board meeting \| board meeting", r"intimation of board meeting", r"\bto consider\b",
                              r"to announce .*results on", r"prior intimation"]),
    ("agm_egm", [r"\bagm\b", r"\begm\b", r"annual general meeting", r"extra-?ordinary general meeting", r"postal ballot",
                 r"scrutini[sz]er", r"voting results", r"e-?voting"]),
    ("routine_compliance", [r"newspaper", r"trading window", r"reg(ulation|\.)\s*74", r"share certificate",
                            r"esop|esos|esps|stock option", r"compliance certificate", r"statutory auditor",
                            r"registrar|\brta\b", r"commercial paper", r"half yearly report", r"financial statements",
                            r"udin", r"loss of|duplicate", r"shareholding pattern", r"secretarial", r"investor complaints",
                            r"book closure", r"certificate pursuant", r"allotment of equity shares", r"special window",
                            r"esg rating", r"\besg\b", r"\biepf\b", r"reg\.? 34 \(1\) annual report", r"\bbrsr\b|business responsibility",
                            r"change in registered office", r"memorandum (&|and) articles", r"statement of deviation",
                            r"reg\.? 32", r"intimation of (record|cut-?off) date for (e-?voting|agm)"]),
    # Another company's results (a parent's, e.g. Suzuki for Maruti) are context, not this company's results.
    ("other", [r"results? (issued|published|announced|declared) by (?!the company)"]),
    ("results", [r"^result \|", r"financial results", r"\bresults? for the (quarter|half|year)"]),
    ("mgmt_change", [r"change in management", r"change in directorate", r"resignation", r"cessation", r"retirement",
                     r"appointment of", r"key managerial", r"re-?appointment", r"senior management personnel"]),
    ("m_and_a", [r"acquisition", r"amalgamation", r"merger", r"scheme of arrangement", r"business transfer",
                 r"share purchase agreement", r"demerger", r"divest", r"disinvestment", r"\bsale of\b", r"joint venture"]),
    ("fundraise", [r"issue of securities", r"\bqip\b", r"qualified institutional placement", r"preferential",
                   r"rights issue", r"\bncds?\b", r"debentures?", r"fund ?rais", r"initial public offer|\bipo\b",
                   r"listing of equity shares"]),
    ("order_win", [r"(bagging|receiving|award|receipt) of order", r"order\(s\) or contract", r"\border win", r"bags? .*order"]),
    ("credit_rating", [r"credit rating", r"\brating\b"]),
    ("dividend_action", [r"dividend", r"buy-?back", r"\bbonus\b", r"stock split|sub-division", r"record date"]),
    ("litigation_regulatory", [r"litigation", r"penalt", r"show cause", r"sebi order", r"tax demand", r"\bgst\b",
                               r"income tax", r"rumou?r", r"clarification", r"insolvency|cirp", r"regulatory action",
                               r"\bfine\b", r"dispute", r"labor certification", r"warning letter", r"form 483|\busfda\b.{0,40}observation"]),
    ("insider_pledge", [r"pledge", r"\bsast\b", r"insider trading", r"encumbrance", r"trading plan"]),
    ("investor_meet", [r"analyst", r"investor meet", r"investor conference", r"earnings call", r"conference call",
                       r"audio recording", r"investor presentation", r"earnings presentation", r"results presentation",
                       r"analyst presentation", r"transcript", r"meeting updates", r"investor call", r"media statement"]),
]
_COMPILED = [(t, [re.compile(p) for p in pats]) for t, pats in RULES]


def rule_classify(category: str | None, subcategory: str | None, subject: str | None) -> str | None:
    text = f"{category or ''} | {subcategory or ''} | {subject or ''}".lower()
    for t, pats in _COMPILED:
        if any(p.search(text) for p in pats):
            return t
    return None


class TypedItem(BaseModel):
    id: int
    type: FilingType


class TypedBatch(BaseModel):
    items: list[TypedItem]


BATCH = 40


def filing_text(f: dict, limit: int = 600) -> str:
    body = (f.get("body") or "").strip()
    return (f"Category: {f.get('category_raw') or ''}\nSub-category: {f.get('subcategory_raw') or ''}\n"
            f"Subject: {f.get('subject') or ''}\nSummary: {body}")[:limit]


def classify(filings: list[dict], use_llm: bool = True) -> dict[int, tuple[str, str]]:
    """{filing_id: (type, source)} where source is rule | llm | fallback. Never raises: a failed batch falls back
    to 'other' so the filing still appears (at worth-a-look weight) rather than vanishing."""
    out, unmatched = {}, []
    for f in filings:
        t = rule_classify(f.get("category_raw"), f.get("subcategory_raw"), f.get("subject"))
        if t:
            out[f["id"]] = (t, "rule")
        else:
            unmatched.append(f)
    for i in range(0, len(unmatched), BATCH):
        chunk = unmatched[i:i + BATCH]
        got: dict[int, str] = {}
        if use_llm:
            try:
                from ..llm import router
                doc = "\n\n".join(f"[ID {f['id']}]\n{filing_text(f)}" for f in chunk)
                r = router.call("classify", "classify", {"types": TYPES, "document": doc}, TypedBatch)
                ids = {f["id"] for f in chunk}
                got = {x.id: x.type for x in r.data.items if x.id in ids}
            except Exception as e:  # noqa: BLE001
                log.warning("classify batch failed: %s", e)
        for f in chunk:
            out[f["id"]] = (got[f["id"]], "llm") if f["id"] in got else ("other", "fallback")
    return out
