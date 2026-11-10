"""score = type_weight × magnitude × novelty × thesis_multiplier, capped at 1. Every factor and the reason behind it
is stored in score_breakdown so the card can answer "why am I seeing this?". Pure Python.

Buckets: attention >= 0.7, worth_a_look >= 0.3, else routine. Nothing is ever deleted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

TYPE_WEIGHTS = {
    "results": 1.0, "governance": 0.95, "m_and_a": 0.95, "mgmt_change": 0.9, "fundraise": 0.8,
    "credit_rating": 0.75, "order_win": 0.7, "litigation_regulatory": 0.7, "insider_pledge": 0.7,
    "dividend_action": 0.4, "investor_meet": 0.25, "agm_egm": 0.2, "board_meeting_notice": 0.15,
    "routine_compliance": 0.05, "other": 0.3,
}
SENIOR_WEIGHT = 1.0
SENIOR_RE = re.compile(r"\b(ceo|cfo|chief executive officer|chief financial officer|managing director|md|"
                       r"whole[- ]time director|auditor)\b", re.I)
FEEDBACK_BOUND = 0.15
BUCKETS = [("attention", 0.7), ("worth_a_look", 0.3)]

# Within-type materiality read from the filing's own words; first match wins. A rating re-affirmed, a shell subsidiary
# incorporated or a director re-appointed is the same filing type as a downgrade, an acquisition or a resignation, but
# not the same news (Step 8: each cue fixed a real mis-ranked filing).
MAGNITUDE_CUES: dict[str, list[tuple[str, float, str]]] = {
    "credit_rating": [(r"downgrad|negative|watch with negative|credit watch", 1.2, "rating downgraded or outlook negative"),
                      (r"upgrad|positive", 1.0, "rating upgraded or outlook positive"),
                      (r"re-?affirm|reaffirm|retain", 0.5, "rating re-affirmed (no change)"),
                      (r".", 0.8, "rating action; direction not stated in the filing summary")],
    "m_and_a": [(r"(secur|acquir|complet|gain)\w*.{0,40}\b(majority|controlling)\b|completion of (the )?acquisition", 1.2,
                 "control secured or acquisition completed"),
                (r"incorporat(ed|ion)\b.{0,80}subsidiary|wholly[- ]owned subsidiary.{0,40}incorporat", 0.4,
                 "new shell subsidiary, not an acquisition")],
    # L&T-style size labels; the company defines them by order value bands.
    "order_win": [(r"\(mega\*?\)|\bmega\*? orders?\b", 1.2, "company calls it a mega order"),
                  (r"\(major\*?\)|\bmajor\*? orders?\b", 1.0, "company calls it a major order"),
                  (r"\(large\*?\)|\blarge\*? orders?\b", 0.8, "company calls it a large order"),
                  (r"\(significant\*?\)|\bsignificant\*? orders?\b", 0.6, "company calls it a significant order")],
    "mgmt_change": [(r"^(?!.*(resign|cessation|retire)).*re-?appoint", 0.6, "re-appointment of a sitting director")],
    "other": [(r"store opening|opening of (a )?(new )?store", 0.5, "routine store opening")],
    "insider_pledge": [(r"release of (pledge|encumbrance)|revocation of pledge", 0.6, "pledge released, not created"),
                       (r"reg(ulation|\.)?\s*31\s*\(4\)", 0.1, "annual no-encumbrance declaration"),
                       (r"reg(ulation|\.)?\s*29\s*\([12]\)", 0.8, "a large holder's stake changed (SAST 29)"),
                       (r"reg(ulation|\.)?\s*31\s*\(1\)", 0.8, "promoter encumbrance disclosure (SAST 31)"),
                       (r"trading plan", 0.6, "insider's pre-declared trading plan")],
}
SAME_EVENT = {"results": timedelta(days=3)}
SAME_EVENT_DEFAULT = timedelta(hours=12)
FOLLOW_UP_RE = re.compile(r"\b(clarification|further to (our|the)|in continuation|update on|addendum|corrigendum|revised)\b", re.I)
ORDER_VALUE_RE = re.compile(r"(₹|\brs\.?|\binr\b|\busd\b|\$|€)\s?[\d,.]+|\b[\d,.]+\s?(crore|cr\b|lakh|million|billion|mn\b|bn\b)", re.I)
REACTION_MOVE = 5.0
REACTION_BOOST = 1.5
REACTION_MIN_WEIGHT = 0.3     # procedural filings never earn a reaction: they didn't cause it
REACTION_MAX_GAP_DAYS = 4


@dataclass
class Breakdown:
    type: str
    type_weight: float
    magnitude: float = 1.0
    novelty: float = 1.0
    thesis_multiplier: float = 1.0
    reasons: list[str] = field(default_factory=list)
    feedback_adjustment: float = 0.0

    @property
    def score(self) -> float:
        return min(1.0, round(self.type_weight * self.magnitude * self.novelty * self.thesis_multiplier, 3))

    @property
    def bucket(self) -> str:
        return bucket_for(self.score)

    def as_dict(self) -> dict:
        return {"type": self.type, "type_weight": self.type_weight, "feedback_adjustment": self.feedback_adjustment,
                "magnitude": self.magnitude, "novelty": self.novelty, "thesis_multiplier": self.thesis_multiplier,
                "score": self.score, "bucket": self.bucket, "reasons": self.reasons,
                "formula": "type_weight × magnitude × novelty × thesis_multiplier (capped at 1)"}


def bucket_for(score: float) -> str:
    for name, th in BUCKETS:
        if score >= th:
            return name
    return "routine"


def type_weight(ftype: str, senior: bool = False, feedback: dict | None = None) -> tuple[float, float]:
    """(weight, feedback adjustment). Feedback (Phase 10) nudges a type's weight, bounded to ±0.15."""
    base = SENIOR_WEIGHT if (ftype == "mgmt_change" and senior) else TYPE_WEIGHTS.get(ftype, 0.3)
    adj = max(-FEEDBACK_BOUND, min(FEEDBACK_BOUND, (feedback or {}).get(ftype, 0.0)))
    return round(max(0.0, base + adj), 3), adj


def is_senior(text: str, people: list[dict] | None = None) -> bool:
    if people:
        return any(SENIOR_RE.search(p.get("role") or "") for p in people)
    return bool(SENIOR_RE.search(text or ""))


def text_magnitude(ftype: str, text: str) -> tuple[float, str | None]:
    for pat, m, why in MAGNITUDE_CUES.get(ftype, []):
        if re.search(pat, text or "", re.I | re.S):
            return m, why
    return 1.0, None


def results_magnitude(pat_yoy: float | None) -> tuple[float, str]:
    """0.8 .. 1.0, rising with the size of the profit change (|PAT YoY| up to 30 %)."""
    if pat_yoy is None:
        return 0.9, "results figures not extracted"
    return round(0.8 + 0.2 * min(1.0, abs(pat_yoy) / 30.0), 3), f"PAT {pat_yoy:+.1f}% YoY"


def order_magnitude(order_cr: float | None, ttm_revenue_cr: float | None) -> tuple[float, str] | None:
    """An order worth 10 %+ of trailing revenue is full weight; 1 % is a quarter of it."""
    if not order_cr or not ttm_revenue_cr:
        return None
    share = order_cr / ttm_revenue_cr
    return round(min(1.3, 0.25 + 7.5 * share), 3), f"order ≈ {share * 100:.1f}% of trailing revenue"


def novelty(ftype: str, filed_at: datetime, filing_id: int, category: str | None, subject: str | None,
            siblings: list[dict], body: str | None = None) -> tuple[float, str | None]:
    """siblings: other filings of the same company and type, each {id, filed_at, category_raw}.
    One event often arrives as several filings ("Result" + "Outcome of board meeting"): only the primary keeps 1.0
    (for results the one filed under the Result category, else the earliest). A follow-up or clarification of an
    earlier filing is 0.5."""
    window = SAME_EVENT.get(ftype, SAME_EVENT_DEFAULT)
    group = [s for s in siblings if abs(s["filed_at"] - filed_at) <= window and s["id"] != filing_id]
    if group:
        me = {"id": filing_id, "filed_at": filed_at, "category_raw": category, "subject": subject, "body": body}
        if ftype == "results":
            key = lambda o: (o["category_raw"] != "Result", o["filed_at"], o["id"])  # noqa: E731
        else:
            # The filing that says the most leads the event ("secures majority stake" over "update on the offer").
            key = lambda o: (-text_magnitude(ftype, f"{o.get('subject') or ''} {o.get('body') or ''}")[0],  # noqa: E731
                             o["filed_at"], o["id"])
        primary = min(group + [me], key=key)
        if primary["id"] != filing_id:
            return (0.25, "duplicate filing of the same results") if ftype == "results" else \
                   (0.5, "another filing about the same event")
    # A follow-up is only a follow-up if we have shown the thing it follows. Without an earlier filing of the same
    # type in the last 45 days, "Update on the takeover offer" is the first the investor hears of it.
    earlier = [s for s in siblings if s["id"] != filing_id and timedelta(0) < filed_at - s["filed_at"] <= timedelta(days=45)]
    if earlier and (FOLLOW_UP_RE.search(subject or "") or FOLLOW_UP_RE.search((body or "")[:80])):
        return 0.5, "follow-up of an earlier filing"
    return 1.0, None


def reaction_session(filed_at: datetime, trading_days: list[date]) -> date | None:
    """First session that could trade on the filing (same day before the 15:30 IST close, else next session),
    limited to sessions that exist in the price data."""
    from ..fiscal import first_session_after
    target = first_session_after(filed_at)
    later = [d for d in trading_days if d >= target]
    if not later or (later[0] - target).days > REACTION_MAX_GAP_DAYS:
        return None
    return later[0]


def compute(ftype: str, *, filed_at: datetime, filing_id: int, category: str | None, subject: str | None,
            body: str | None, siblings: list[dict], people: list[dict] | None = None, pat_yoy: float | None = None,
            results_extracted: bool = False, order: tuple[float, str] | None = None, reacted: dict | None = None,
            results_same_day: bool = False, thesis: tuple[float, str] | None = None,
            feedback: dict | None = None) -> Breakdown:
    text = f"{subject or ''} {body or ''}"
    senior = ftype == "mgmt_change" and is_senior(text, people)
    w, adj = type_weight(ftype, senior, feedback)
    b = Breakdown(type=ftype, type_weight=w, feedback_adjustment=adj)
    if senior:
        b.reasons.append("CEO / CFO / MD / auditor involved")
    if adj:
        b.reasons.append(f"your feedback adjusted this type by {adj:+.2f}")
    if ftype == "results" and results_extracted:
        b.magnitude, why = results_magnitude(pat_yoy)
        b.reasons.append(why)
    elif order:
        b.magnitude, why = order
        b.reasons.append(why)
    else:
        m, why = text_magnitude(ftype, text)
        if why:
            b.magnitude = m
            b.reasons.append(why)
        elif ftype == "mgmt_change" and not senior:
            b.magnitude = 0.85
        elif ftype == "order_win" and not ORDER_VALUE_RE.search(text):
            b.magnitude = 0.8
            b.reasons.append("order value not stated")
    b.novelty, why = novelty(ftype, filed_at, filing_id, category, subject, siblings, body)
    if why:
        b.reasons.append(why)
    # The market's own verdict: credited only to a primary filing of a substantive type, and on a results day only
    # to the results filing (the reaction belongs to the numbers).
    if (reacted and reacted.get("hit") and b.novelty == 1.0 and TYPE_WEIGHTS.get(ftype, 0) >= REACTION_MIN_WEIGHT
            and (ftype == "results" or not results_same_day)):
        b.magnitude = round(b.magnitude * REACTION_BOOST, 3)
        z = f", {reacted['z']:.1f}σ for the stock" if reacted.get("z") else ""
        b.reasons.append(f"stock moved {reacted['move_pct']:+.1f}% in the first session after the filing{z}")
    if thesis:
        b.thesis_multiplier, why = thesis
        b.reasons.append(why)
    return b
