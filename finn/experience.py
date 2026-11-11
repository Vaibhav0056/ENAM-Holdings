"""Experience extras (spec E3, E5, E7): time-travel replay, decision journal, feedback learning loop.

- Replay: the feed exactly as it would have looked at a past moment (only filings filed by then), plus what each
  surfaced company's stock did over the next sessions: a backtest of the filter.
- Journal: "Acting on this" + a one-line reason, shown later next to everything that happened after.
- Feedback: Useful / Not useful / Should be higher on a card nudges that filing type's weight, bounded to ±0.15
  (spec §5), stored in user_state.prefs and logged; recent cards are re-scored at once.
"""
from __future__ import annotations

import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from .database import read_session_run, session
from .feed import digest as dg
from .fiscal import IST, to_ist
from .models import Company, Enrichment, Feedback, Filing, JournalEntry, Price, UserState

router = APIRouter()
STEP = 0.05          # weight change per net signal
BOUND = 0.15
SIGNALS = {"useful": 0.5, "not_useful": -1.0, "higher": 1.0}


# ---------- feedback ----------

def adjustments(rows: list[tuple[str, str]]) -> dict[str, float]:
    """rows: (filing type, signal) -> {type: bounded weight adjustment}. Pure."""
    net = defaultdict(float)
    for t, sig in rows:
        net[t] += SIGNALS.get(sig, 0.0)
    return {t: round(max(-BOUND, min(BOUND, STEP * v)), 3) for t, v in net.items() if v}


@router.post("/api/v2/cards/{filing_id}/feedback")
async def feedback(filing_id: int, request: Request):
    sig = (await request.json()).get("signal")
    if sig not in SIGNALS:
        raise HTTPException(400, f"signal must be one of {sorted(SIGNALS)}")
    with session() as s:
        e = s.get(Enrichment, filing_id)
        if not e:
            raise HTTPException(404, "no such card")
        s.add(Feedback(filing_id=filing_id, signal=sig))
        s.flush()
        rows = s.execute(select(Enrichment.type, Feedback.signal).join(Feedback, Feedback.filing_id == Enrichment.filing_id)).all()
        adj = adjustments([(t, g) for t, g in rows])
        st = s.get(UserState, 1) or UserState(id=1, prefs={})
        prefs = dict(st.prefs or {})
        log = list(prefs.get("feedback_log") or [])[-49:]
        log.append({"at": datetime.now(timezone.utc).isoformat(), "filing_id": filing_id, "type": e.type, "signal": sig,
                    "adjustment_after": adj.get(e.type, 0.0)})
        prefs.update(type_weight_adjustments=adj, feedback_log=log)
        st.prefs = prefs
        s.merge(st)
        ftype = e.type

    def rescore():
        from .feed import pipeline
        pipeline.run(rescore_all=False, use_llm=False, enrich=False)
        dg.invalidate()
    threading.Thread(target=rescore, daemon=True).start()
    return {"ok": True, "type": ftype, "adjustment": adj.get(ftype, 0.0), "bound": BOUND}


@router.get("/api/v2/feedback")
def feedback_state():
    def q(s):
        st = s.get(UserState, 1)
        p = (st.prefs or {}) if st else {}
        return {"adjustments": p.get("type_weight_adjustments") or {}, "log": (p.get("feedback_log") or [])[-20:]}
    return read_session_run(q)


# ---------- journal ----------

@router.post("/api/v2/journal")
async def journal_add(request: Request):
    b = await request.json()
    reason = (b.get("reason") or "").strip()
    action = (b.get("action") or "acting").strip()[:40]
    if not 3 <= len(reason) <= 300:
        raise HTTPException(400, "write a one-line reason (3-300 characters)")
    with session() as s:
        c = s.scalar(select(Company).where(Company.nse_symbol == (b.get("symbol") or "").upper()))
        if not c:
            raise HTTPException(404, "unknown company")
        s.add(JournalEntry(company_id=c.id, filing_id=b.get("filing_id"), action=action, reason=reason))
    return {"ok": True}


@router.get("/api/v2/journal")
def journal(symbol: str | None = None):
    def q(s):
        qq = select(JournalEntry, Company.nse_symbol).join(Company, Company.id == JournalEntry.company_id).order_by(JournalEntry.at.desc())
        if symbol:
            qq = qq.where(Company.nse_symbol == symbol.upper())
        out = []
        for j, sym in s.execute(qq.limit(50)).all():
            after = s.execute(select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
                              .where(Filing.company_id == j.company_id, Filing.filed_at > j.at, Enrichment.bucket == "attention")
                              .order_by(Filing.filed_at).limit(5)).all()
            out.append({"id": j.id, "symbol": sym, "action": j.action, "reason": j.reason, "at": to_ist(j.at).isoformat(),
                        "filing_id": j.filing_id,
                        "since": [{"id": f.id, "filed_at": to_ist(f.filed_at).isoformat(), "headline": e.headline, "type": e.type}
                                  for f, e in after]})
        return out
    return {"entries": read_session_run(q)}


# ---------- time-travel replay ----------

def forward_move(rows: list[tuple[date, float]], start: date, sessions: int = 5) -> dict | None:
    """Close-to-close move from the last session on/before `start` to `sessions` sessions later. Pure."""
    before = [r for r in rows if r[0] <= start]
    after = [r for r in rows if r[0] > start]
    if not before or len(after) < 1:
        return None
    base, end = before[-1], after[min(sessions, len(after)) - 1]
    return {"from": base[0].isoformat(), "to": end[0].isoformat(), "sessions": min(sessions, len(after)),
            "move_pct": round((end[1] / base[1] - 1) * 100, 2)}


@router.get("/api/v2/replay")
def replay(as_of: str, window: str = "7d", sessions: int = 5):
    try:
        d = date.fromisoformat(as_of)
    except ValueError:
        raise HTTPException(400, "as_of must be YYYY-MM-DD")
    if d >= date.today() or d < date.today() - timedelta(days=90):
        raise HTTPException(400, "pick a date in the last 90 days")
    view = dg.digest(window, as_of=as_of)

    def q(s):
        prices = defaultdict(list)
        for sym, dd, c in s.execute(select(Company.nse_symbol, Price.date, Price.adj_close).join(Company, Company.id == Price.company_id)
                                    .order_by(Price.date)):
            prices[sym].append((dd, c))
        return prices
    prices = read_session_run(q)
    moves = {}
    for c in view["attention"] + view["worth_a_look"]:
        if c["symbol"] not in moves:
            moves[c["symbol"]] = forward_move(prices.get(c["symbol"], []), d, sessions)
    big = sorted(((sym, m) for sym, m in moves.items() if m), key=lambda x: -abs(x[1]["move_pct"]))
    flagged_before_big_move = [{"symbol": sym, **m, "cards": [{"id": x["id"], "headline": x["headline"], "bucket": x["bucket"]}
                                                    for x in view["attention"] + view["worth_a_look"] if x["symbol"] == sym][:3]}
                               for sym, m in big if abs(m["move_pct"]) >= 5]
    return {"as_of": as_of, "window": view["window"], "counts": view["counts"], "attention": view["attention"],
            "worth_a_look": view["worth_a_look"], "moves_after": moves, "flagged_before_big_move": flagged_before_big_move,
            "note": f"Only filings filed by {as_of} (end of day, IST); moves are the next {sessions} sessions, adjusted closes."}
