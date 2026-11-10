"""Push pipeline: classify -> score -> (enrich, Phase 3) -> (thesis impact, Phase 5) -> one-liners.

    python -m finn.feed.pipeline            # new filings + re-score the last 5 days
    python -m finn.feed.pipeline --all      # re-score everything (after a scoring change)

Fail soft: a filing that can't be processed is marked failed with the reason and still shown at worth-a-look weight.
Routine items never get an LLM call. Scores are recomputed without re-reading anything, so re-runs are cheap.
"""
from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import select

from ..database import session
from ..marketmath import index_moves, move_check
from ..models import Company, Enrichment, Filing, Price, UserState
from . import classify as cls
from . import score as sc

log = logging.getLogger("finn.feed.pipeline")
RESCORE_DAYS = 5
PROMPT_VERSION = "push@v1"

_BOILER = [r"^announcement under regulation 30 \(lodr\)\s*-\s*", r"^board meeting outcome for\s*",
           r"^announcement under regulation 30 of sebi lodr\s*-\s*", r"^disclosure under regulation 30 of sebi.*?2015\s*-?\s*",
           r"^intimation (under|of)\s*"]
_GENERIC = re.compile(r"^(press release / media release|general-updates|company update|disclosure under regulation 30.*|"
                      r"outcome of (the )?board meeting( held on .*)?|updates?)\.?$", re.I)


_ABBREV = re.compile(r"\b(?:(?i:Mr|Ms|Mrs|Dr|Shri|Smt|No|Ltd|Pvt|Co|Inc|i\.e|e\.g|viz|Rs|Sr|Jr|St|vs|w\.e\.f)|[A-Z]|[A-Z][a-z]{1,4}(?=\.\s+[A-Z]\.))\.")
_LEAD_BOILER = re.compile(
    r"^(?:(?:this is to|we wish to|we would like to|we hereby|kindly be) inform(?:ed)? (?:you )?that,?\s*|"
    r"pursuant to (?:regulation|reg\.?) 30[^,]*,\s*(?:we (?:would like to |wish to )?inform (?:you )?that,?\s*)?|"
    r"in (?:compliance|accordance) with[^,]*,\s*|further to our (?:letter|intimation)[^,]*,\s*)", re.I)
_EMPTY_BODY = re.compile(r"^(as per (the )?(annexure|attachment|attached|enclosed).*|please find (the )?(enclosed|attached).*|"
                         r"enclosed.*|attached.*|(the )?(company|exchange) (has )?(informed|submitted).{0,30}\.?)$", re.I)


def first_sentence(body: str | None) -> str:
    text = re.sub(r"\s+", " ", body or "").strip()
    protected = _ABBREV.sub(lambda m: m.group(0).replace(".", "\x00"), text)
    sentence = re.split(r"(?<=[.!?])\s+(?=[A-Z])", protected)[0].replace("\x00", ".")
    sentence = _LEAD_BOILER.sub("", sentence).strip()
    return "" if _EMPTY_BODY.match(sentence) else sentence


def _norm(x: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()


def headline(subject: str | None, body: str | None, subcategory: str | None = None) -> str:
    """Readable headline from BSE boilerplate. A subject that is generic, or that only repeats BSE's sub-category
    ("Change in Management", "Credit Rating"), falls back to the first sentence of BSE's own summary."""
    s = (subject or "").strip()
    for p in _BOILER:
        s = re.sub(p, "", s, flags=re.I).strip()
    if not s or _GENERIC.match(s) or (subcategory and _norm(s) == _norm(subcategory)):
        first = first_sentence(body)
        if first and len(first) > 12:
            s = first
        else:
            s = s or (subject or "Filing")
    return (s[:1].upper() + s[1:])[:180]


# ---------- one-liners (worth a look only) ----------

class Line(BaseModel):
    id: int
    line: str


class LineBatch(BaseModel):
    items: list[Line]


def fallback_line(f: dict) -> str:
    raw = (f.get("body") or f.get("subject") or "").strip()
    words = raw.split()
    return " ".join(words[:20]) + ("…" if len(words) > 20 else "")


def one_liners(items: list[dict], use_llm: bool = True) -> dict[int, tuple[str, str]]:
    """{id: (line, source)} where source is llm | filing. Batched, 40 per call."""
    out: dict[int, tuple[str, str]] = {}
    for i in range(0, len(items), cls.BATCH):
        chunk = items[i:i + cls.BATCH]
        got: dict[int, str] = {}
        if use_llm:
            try:
                from ..llm import router
                doc = "\n\n".join(f"[ID {f['id']}]\n{cls.filing_text(f, 900)}" for f in chunk)
                r = router.call("one_liner", "one_liner", {"document": doc}, LineBatch)
                ids = {f["id"] for f in chunk}
                got = {x.id: x.line.strip() for x in r.data.items if x.id in ids and x.line.strip()}
            except Exception as e:  # noqa: BLE001
                log.warning("one-liner batch failed: %s", e)
        for f in chunk:
            out[f["id"]] = (got[f["id"]], "llm") if f["id"] in got else (fallback_line(f), "filing")
    return out


# ---------- data loading ----------

def _filing_dict(f: Filing) -> dict:
    return {"id": f.id, "company_id": f.company_id, "filed_at": f.filed_at, "category_raw": f.category_raw,
            "subcategory_raw": f.subcategory_raw, "subject": f.subject, "body": f.body}


def _prices(s) -> tuple[dict[int, list[dict]], dict]:
    rows = s.execute(select(Price.company_id, Price.symbol, Price.date, Price.adj_close, Price.close)
                     .order_by(Price.symbol, Price.date)).all()
    by_company: dict[int, list[dict]] = defaultdict(list)
    index = []
    for cid, sym, d, adj, close in rows:
        r = {"date": d, "close": adj or close}
        if sym == "^NSEI":
            index.append(r)
        elif cid:
            by_company[cid].append(r)
    return by_company, index_moves(index)


def reaction(filed_at: datetime, rows: list[dict], market: dict) -> dict | None:
    if not rows:
        return None
    session_day = sc.reaction_session(filed_at, [r["date"] for r in rows])
    if session_day is None:
        return None
    upto = [r["close"] for r in rows if r["date"] <= session_day]
    return move_check(upto, sc.REACTION_MOVE, market.get(session_day))


def _feedback_prefs(s) -> dict:
    st = s.get(UserState, 1)
    return ((st.prefs or {}).get("type_weight_adjustments") or {}) if st else {}


# ---------- the run ----------

_RESULTS_DAY_SUBCATS = {"outcome of board meeting", "press release / media release"}
_BARE_BODY = re.compile(r"^(press release( dated [\w ,]+)?|outcome of (the )?board meeting.*|announcement is attached|"
                        r"board meeting outcome.*)\.?$", re.I)
_RESULTS_WORDS = re.compile(r"\bresults?\b|\bprofit\b|\bq[1-4]\s?fy|\bquarter(ly)?\b|\bebitda\b|\brevenue\b", re.I)


def results_day_pass() -> int:
    """On a company's results day, a bare "Outcome of Board Meeting" or "Press Release" filing (no content of its own
    in BSE's summary) or one that talks about results/profit is part of the results event, not "other".
    Typing it `results` lets novelty fold it under the primary results card instead of scoring it separately."""
    n = 0
    with session() as s:
        rows = s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)).all()
        days = {(f.company_id, to_ist_date(f.filed_at)) for f, e in rows if e.type == "results"}
        for f, e in rows:
            if e.type in ("results",) or e.type_source == "rule" and e.type not in ("other", "investor_meet"):
                continue
            if (f.subcategory_raw or "").strip().lower() not in _RESULTS_DAY_SUBCATS:
                continue
            if (f.company_id, to_ist_date(f.filed_at)) not in days:
                continue
            body = re.sub(r"\s+", " ", f.body or "").strip()
            if _BARE_BODY.match(body) or _RESULTS_WORDS.search(body):
                e.type, e.type_source = "results", "rule"
                n += 1
    return n


def to_ist_date(dt: datetime):
    from ..fiscal import to_ist
    return to_ist(dt).date()


def _thesis_factor(impact: str | None) -> tuple[float, str] | None:
    from ..thesis.guardian import multiplier
    return multiplier(impact)


def _order_factor(payload: dict) -> tuple[float, str] | None:
    if payload.get("kind") != "order":
        return None
    return sc.order_magnitude(payload.get("value_cr"), payload.get("ttm_revenue_cr"))


def score_pass(stats: dict, rescore_all: bool, since: datetime, new_ids: list[int]) -> None:
    """Score every filing in scope (pure Python, repeatable). Enriched headlines are kept; others are rebuilt."""
    with session() as s:
        by_company, market = _prices(s)
        feedback = _feedback_prefs(s)
        q = select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
        if not rescore_all:
            q = q.where((Filing.filed_at >= since) | (Filing.id.in_(new_ids or [-1])))
        todo = s.execute(q).all()
        siblings: dict[tuple[int, str], list[dict]] = defaultdict(list)
        results_days = set()
        for f, e in s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)).all():
            siblings[(f.company_id, e.type)].append({"id": f.id, "filed_at": f.filed_at, "category_raw": f.category_raw,
                                                     "subject": f.subject, "body": f.body})
            if e.type == "results":
                results_days.add((f.company_id, to_ist_date(f.filed_at)))
        for f, e in todo:
            try:
                payload = e.payload or {}
                b = sc.compute(
                    e.type, filed_at=f.filed_at, filing_id=f.id, category=f.category_raw, subject=f.subject, body=f.body,
                    siblings=siblings[(f.company_id, e.type)], people=payload.get("people") or None,
                    pat_yoy=payload.get("pat_yoy"), results_extracted=bool(payload.get("ok") and payload.get("table")),
                    order=_order_factor(payload),
                    reacted=reaction(f.filed_at, by_company.get(f.company_id, []), market),
                    results_same_day=(f.company_id, to_ist_date(f.filed_at)) in results_days and e.type != "results",
                    thesis=_thesis_factor(e.thesis_impact), feedback=feedback)
                e.score, e.bucket, e.score_breakdown = b.score, b.bucket, b.as_dict()
                if not payload.get("ok") or e.type not in ("results", "mgmt_change"):
                    e.headline = headline(f.subject, f.body, f.subcategory_raw)
                f.status, f.error = "processed", None
                stats["scored"] += 1
                stats["buckets"][b.bucket] += 1
            except Exception as ex:  # noqa: BLE001 - one bad filing never stops the run
                log.exception("scoring failed for filing %s", f.id)
                f.status, f.error = "failed", f"couldn't process: {ex}"[:500]
                e.score, e.bucket = max(e.score or 0, 0.3), "worth_a_look"
                e.score_breakdown = {"error": str(ex)[:200], "reasons": ["couldn't process; shown so it isn't missed"]}
                stats["failed"] += 1


def share_event_payloads() -> int:
    """One event, several filings: the extractor may find the people in a twin rather than in the filing that leads
    the event. The leading card (novelty 1.0) borrows the richest twin's payload and headline, so the card the
    investor sees is the informative one. Returns how many cards borrowed."""
    n = 0
    with session() as s:
        rows = s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
                         .where(Enrichment.type == "mgmt_change")).all()
        by_co: dict[int, list] = defaultdict(list)
        for f, e in rows:
            by_co[f.company_id].append((f, e))
        for items in by_co.values():
            for f, e in items:
                if (e.score_breakdown or {}).get("novelty") != 1.0 or (e.payload or {}).get("people"):
                    continue
                twins = [(tf, te) for tf, te in items if tf.id != f.id
                         and abs(tf.filed_at - f.filed_at) <= sc.SAME_EVENT_DEFAULT and (te.payload or {}).get("people")]
                if twins:
                    tf, te = max(twins, key=lambda t: len(t[1].payload["people"]))
                    e.payload = {**te.payload, "borrowed_from_filing": tf.id}
                    e.headline = te.headline
                    n += 1
    return n


def run(rescore_all: bool = False, use_llm: bool = True, reclassify_llm: bool = False, enrich: bool = True) -> dict:
    stats = {"new": 0, "classified_by": defaultdict(int), "scored": 0, "failed": 0, "one_liners": defaultdict(int),
             "buckets": defaultdict(int)}
    with session() as s:
        new = [_filing_dict(f) for f in s.scalars(select(Filing).where(~Filing.enrichment.has()).order_by(Filing.filed_at))]
    stats["new"] = len(new)
    types = cls.classify(new, use_llm=use_llm)
    with session() as s:
        for f in new:
            t, src = types.get(f["id"], ("other", "fallback"))
            stats["classified_by"][src] += 1
            s.add(Enrichment(filing_id=f["id"], type=t, type_source=src, score=0.0, bucket="worth_a_look",
                             headline=headline(f["subject"], f["body"], f["subcategory_raw"]), prompt_version=PROMPT_VERSION))

    if rescore_all:  # a rules change applies to history too; deterministic and free
        with session() as s:
            for f, e in s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)).all():
                t = cls.rule_classify(f.category_raw, f.subcategory_raw, f.subject)
                if t and (t != e.type or e.type_source != "rule"):
                    e.type, e.type_source = t, "rule"
                    stats["classified_by"]["rule_reapplied"] += 1
    if reclassify_llm:  # a prompt change: re-ask the model about the filings it typed (new prompt version = new cache key)
        with session() as s:
            redo = [_filing_dict(f) for f, e in s.execute(select(Filing, Enrichment).join(Enrichment)
                                                          .where(Enrichment.type_source.in_(["llm", "fallback"])))]
        again = cls.classify(redo, use_llm=use_llm)
        with session() as s:
            for fid, (t, src) in again.items():
                e = s.get(Enrichment, fid)
                if src != "fallback" and (e.type != t or e.type_source != src):
                    e.type, e.type_source = t, src
                    stats["classified_by"]["llm_reclassified"] += 1
    stats["classified_by"]["results_day"] = results_day_pass()

    since = datetime.now(timezone.utc) - timedelta(days=RESCORE_DAYS)
    new_ids = [f["id"] for f in new]
    score_pass(stats, rescore_all, since, new_ids)
    if enrich:  # enrichment needs pass-1 novelty (which results filing leads its event); pass 2 uses the payloads
        from ..enrich import run as enrich_run
        from ..thesis import guardian
        stats["enriched"] = enrich_run.run(use_llm=use_llm)
        stats["thesis_impacts"] = guardian.impacts(use_llm=use_llm)
        stats["results_day"] = guardian.results_day()
        stats["buckets"] = defaultdict(int)
        stats["scored"] = 0
        score_pass(stats, rescore_all, since, new_ids)
    stats["event_payloads_shared"] = share_event_payloads()
    if enrich:
        from ..thesis import guardian
        stats["thesis_status"] = guardian.statuses()
    with session() as s:
        need_line = [_filing_dict(f) for f, e in s.execute(
            select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
            .where(Enrichment.one_liner.is_(None), Enrichment.bucket.in_(["worth_a_look", "attention"]))).all()
            if e.bucket == "worth_a_look" or not (e.payload or {}).get("ok")]
    if need_line:
        lines = one_liners(need_line, use_llm=use_llm)
        with session() as s:
            for fid, (line, src) in lines.items():
                e = s.get(Enrichment, fid)
                e.one_liner = line
                e.model = src if src == "filing" else "llm"
                stats["one_liners"][src] += 1
    return {k: (dict(v) if isinstance(v, defaultdict) else v) for k, v in stats.items()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    print(run(rescore_all="--all" in sys.argv, use_llm="--no-llm" not in sys.argv,
              reclassify_llm="--reclassify-llm" in sys.argv, enrich="--no-enrich" not in sys.argv))
