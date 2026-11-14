"""Alerts (spec P7). Rules are rows in alert_rules (editable); every alert is deduplicated by the database on
(rule, company, day), so re-running never repeats one. The Alerts tab is the source of truth; push only points to it.

Rules:
- volume_spike      volume >= N × the 20-session average (default 3.0) on a stock whose 20-session average traded
                    value is at least min_avg_value_cr (default ₹10 crore a day); inside one burst only an
                    escalation re-alerts.
- price_move        |move| >= X % or >= 3σ of the stock's own history in excess of the NIFTY 50; suppressed on the
                    stock's ex-dates (a dividend or split is not a move).
- announcement_type material filing types the user picked (results, mgmt change, governance, M&A...).
- new_promise       a newer research report for a holding has N+ promises the previous one didn't, or drops a
                    promise management had repeated (from the Credibility Ledger).
- peer_read_across  a research-only peer filed something material: shown against the holding it is a peer of.

One alert per company per day: every rule records its trigger, then consolidate() keeps the most important one
active (filing > price move > promise change > peer read-across > volume), folds the others into its "also" list and
marks them superseded (kept, never deleted, never shown or pushed).

    python -m finn.alerting.engine                 # evaluate the last LOOKBACK_DAYS
    python -m finn.alerting.engine --reevaluate    # apply the current rules to every stored alert
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from ..database import engine as db_engine
from ..database import session
from ..fiscal import first_session_after, to_ist
from ..marketmath import VOL_WINDOW, avg_traded_value_cr, index_moves, move_check, volume_average
from ..models import Alert, AlertRule, CalendarEvent, Company, Enrichment, Filing, Price, Report

log = logging.getLogger("finn.alerts")
LOOKBACK_DAYS = 10
VOL_COOLDOWN = 3
TYPE_LABEL = {"results": "Results", "mgmt_change": "Management change", "governance": "Governance flag",
              "m_and_a": "M&A", "fundraise": "Fundraise", "order_win": "Order win", "credit_rating": "Credit rating",
              "litigation_regulatory": "Legal / regulatory", "insider_pledge": "Insider / pledge"}


def rules() -> dict[str, AlertRule]:
    with session() as s:
        return {r.kind: r for r in s.scalars(select(AlertRule)) if r.enabled}


def _insert(rule_id: int, company_id: int, d: date, title: str, line: str, link: str, filing_id: int | None = None,
            detail: dict | None = None) -> bool:
    ins = (postgresql.insert if db_engine().dialect.name == "postgresql" else sqlite.insert)(Alert)
    stmt = ins.values(rule_id=rule_id, company_id=company_id, triggered_on=d, title=title[:200], line=line,
                      deep_link=link[:200], filing_id=filing_id, detail=detail).on_conflict_do_nothing(
        index_elements=["rule_id", "company_id", "triggered_on"])
    with session() as s:   # RETURNING: rowcount is unreliable for ON CONFLICT DO NOTHING
        return s.execute(stmt.returning(Alert.id)).first() is not None


# ---------- price / volume (pure over loaded rows) ----------

def volume_hits(rows: list[dict], multiple: float, since: date, min_value_cr: float = 0.0) -> list[dict]:
    """Days whose volume is >= multiple × the 20-session average, on a stock that trades at least min_value_cr a
    day on average (thin stocks spike on noise). Inside one burst only an escalation re-alerts."""
    vols = [r["volume"] for r in rows]
    closes = [r.get("raw_close") or r["close"] for r in rows]
    spikes, base_at, value_at, out = {}, {}, {}, []
    for i in range(1, len(rows)):
        base = volume_average(vols[: i + 1])
        value = avg_traded_value_cr(vols[: i + 1], closes[: i + 1])
        if base and vols[i] and vols[i] / base >= multiple and (value or 0) >= min_value_cr:
            spikes[i], base_at[i], value_at[i] = vols[i] / base, base, value
        if rows[i]["date"] < since or i not in spikes:
            continue
        if spikes[i] > max(spikes.get(j, 0) for j in range(i - VOL_COOLDOWN, i)):  # escalation-only inside a burst
            out.append({"date": rows[i]["date"], "ratio": round(spikes[i], 2), "volume": vols[i],
                        "average": base_at[i], "avg_value_cr": round(value_at[i], 1)})
    return out


def move_hits(rows: list[dict], pct: float, market: dict, since: date, ex_dates: set[date]) -> list[dict]:
    out = []
    closes = [r["close"] for r in rows]
    for i in range(1, len(rows)):
        d = rows[i]["date"]
        if d < since or d in ex_dates:
            continue
        m = move_check(closes[: i + 1], pct, market.get(d))
        if m and m["hit"]:
            out.append({"date": d, **m, "close": closes[i], "prev_close": closes[i - 1]})
    return out


def volume_text(symbol: str, h: dict) -> tuple[str, str]:
    return (f"{symbol}: volume {h['ratio']:.1f}× its 20-day average",
            f"{h['volume'] / 1e5:,.1f} lakh shares vs an average of {h['average'] / 1e5:,.1f} lakh "
            f"(₹{h['avg_value_cr']:,.0f} cr traded a day)")


def _price_alerts(r_vol: AlertRule | None, r_move: AlertRule | None, since: date) -> int:
    n = 0
    with session() as s:
        cos = list(s.scalars(select(Company).where(Company.is_portfolio.is_(True))))
        rows = s.execute(select(Price.company_id, Price.symbol, Price.date, Price.close, Price.adj_close, Price.volume)
                         .order_by(Price.symbol, Price.date)).all()
        ex = defaultdict(set)
        for cid, d in s.execute(select(CalendarEvent.company_id, CalendarEvent.event_date)
                                .where(CalendarEvent.kind.in_(["ex_date", "record_date"]))):
            ex[cid].add(d)
    by_co, index = defaultdict(list), []
    for cid, sym, d, close, adj, vol in rows:
        r = {"date": d, "close": adj or close, "raw_close": close, "volume": vol}
        (index if sym == "^NSEI" else by_co[cid]).append(r)
    market = index_moves(index)
    for c in cos:
        prs = by_co.get(c.id, [])
        if r_vol:
            for h in volume_hits(prs, float(r_vol.params.get("multiple", 3.0)), since,
                                 float(r_vol.params.get("min_avg_value_cr", 10.0))):
                n += _insert(r_vol.id, c.id, h["date"], *volume_text(c.nse_symbol, h),
                             f"/#holding/{c.nse_symbol}", detail=h | {"date": h["date"].isoformat()})
        if r_move:
            for h in move_hits(prs, float(r_move.params.get("pct", 5.0)), market, since, ex[c.id]):
                why = [f"closed ₹{h['close']:,.2f} from ₹{h['prev_close']:,.2f}"]
                if h.get("sigma_pct"):
                    why.append(f"a normal day is ±{h['sigma_pct']:.1f}%")
                if h.get("market_pct") is not None:
                    why.append(f"NIFTY {h['market_pct']:+.1f}%")
                sig = f" ({h['z']:.1f}σ)" if h.get("z") and h["z"] >= 3 else ""
                n += _insert(r_move.id, c.id, h["date"], f"{c.nse_symbol} {'up' if h['move_pct'] > 0 else 'down'} "
                             f"{abs(h['move_pct']):.1f}%{sig}", " · ".join(why), f"/#holding/{c.nse_symbol}",
                             detail={k: (v.isoformat() if isinstance(v, date) else v) for k, v in h.items()})
    return n


# ---------- filings ----------

def card_line(e: Enrichment) -> str:
    p = e.payload or {}
    if p.get("kind") == "results" and p.get("ok"):
        parts = [f"{'Revenue' if r['metric'] == 'revenue' else 'PAT'} {r['yoy']:+.1f}% YoY" for r in p["table"]
                 if r["metric"] in ("revenue", "pat") and r.get("found") and r.get("yoy") is not None]
        rd = (p.get("results_day") or {}).get("comparisons") or []
        if rd:
            x = rd[0]
            parts.append(f"{x['metric']}: promised {x['promised']}, {x['verdict']}")
        if parts:
            return " · ".join(parts)
    if p.get("people"):
        x = p["people"][0]
        return f"{x['name']} ({x.get('role') or 'role not stated'}) {x['direction']}"
    return e.why_it_matters or e.one_liner or e.headline or ""


def _filing_alerts(rule: AlertRule, since_utc: datetime, peers: bool = False) -> int:
    types = set(rule.params.get("types") or [])
    n = 0
    with session() as s:
        q = (select(Filing, Enrichment, Company).join(Enrichment, Enrichment.filing_id == Filing.id)
             .join(Company, Company.id == Filing.company_id)
             .where(Filing.filed_at >= since_utc, Enrichment.type.in_(list(types)), Enrichment.bucket == "attention")
             .order_by(Filing.filed_at))
        q = q.where(Company.peer_of.is_not(None)) if peers else q.where(Company.is_portfolio.is_(True))
        rows = s.execute(q).all()
        holding = {c.id: c for c in s.scalars(select(Company).where(Company.is_portfolio.is_(True)))}
    for f, e, c in rows:
        d = to_ist(f.filed_at).date()
        label = TYPE_LABEL.get(e.type, e.type)
        if peers:
            h = holding.get(c.peer_of)
            if not h:
                continue
            n += _insert(rule.id, h.id, d, f"Peer read-across: {c.nse_symbol} ({label}) → {h.nse_symbol}",
                         f"{c.name}: {card_line(e)}"[:400], f"/#card/{f.id}", f.id, {"peer": c.nse_symbol})
        else:
            n += _insert(rule.id, c.id, d, f"{c.nse_symbol}: {label}", card_line(e)[:400], f"/#card/{f.id}", f.id)
    return n


# ---------- new / dropped promises ----------

def promise_diff(old: list[dict], new: list[dict]) -> tuple[list[dict], list[dict]]:
    """Promises in `new` that `old` didn't have, and repeated promises from `old` that `new` no longer has."""
    from ..research.ledger import same_promise
    added = [p for p in new if not any(same_promise(p, q) for q in old)]
    dropped = [q for q in old if q.get("also_said_in") and not any(same_promise(q, p) for p in new)]
    return added, dropped


def _promise_alerts(rule: AlertRule) -> int:
    n = 0
    with session() as s:
        cos = {c.id: c for c in s.scalars(select(Company).where(Company.is_portfolio.is_(True)))}
        reps = s.execute(select(Report.company_id, Report.id, Report.created_at, Report.data)
                         .where(Report.kind == "core", Report.company_id.in_(list(cos))).order_by(Report.created_at)).all()
    by_co = defaultdict(list)
    for cid, rid, at, data in reps:
        by_co[cid].append((rid, at, data))
    min_new = int(rule.params.get("min_new", 1))
    for cid, rs in by_co.items():
        # Only a genuinely newer call counts: re-running research on the same calls (e.g. after a prompt change) must
        # never look like management changing its story. "New" = said on the newest call and absent before.
        from ..research.ledger import period
        latest = rs[-1]
        newest = lambda data: max((period(x.get("said_in")) for x in data["ledger"] if period(x.get("said_in"))), default=None)  # noqa: E731
        nl = newest(latest[2])
        prev = next((r for r in reversed(rs[:-1]) if newest(r[2]) and nl and newest(r[2]) < nl), None)
        if not prev:
            continue
        fresh = [p for p in latest[2]["ledger"] if period(p.get("said_in")) == nl]
        added, _ = promise_diff(prev[2]["ledger"], fresh)
        _, dropped = promise_diff(prev[2]["ledger"], latest[2]["ledger"])
        if len(added) >= min_new or dropped:
            c = cos[cid]
            bits = []
            if added:
                bits.append(f"{len(added)} new: " + "; ".join(f"{p['metric']} {p['target_text']}" for p in added[:2]))
            if dropped:
                bits.append(f"{len(dropped)} no longer repeated: " + "; ".join(f"{p['metric']} {p['target_text']}" for p in dropped[:2]))
            n += _insert(rule.id, cid, to_ist(latest[1]).date(), f"{c.nse_symbol}: management's promises changed",
                         " · ".join(bits)[:400], f"/#research/{c.nse_symbol}",
                         detail={"added": [p["id"] for p in added], "dropped": [p["id"] for p in dropped],
                                 "report_id": latest[0], "previous_report_id": prev[0]})
    return n


# ---------- one alert per company per day ----------

PRIORITY = {"announcement_type": 0, "price_move": 1, "new_promise": 2, "peer_read_across": 3, "volume_spike": 4}


def _short(title: str) -> str:
    return title.split(": ", 1)[1] if ": " in title else title


def consolidate(since: date | None = None) -> dict:
    """Keep one active alert per (company, day): the highest-priority trigger, with the others listed under
    detail["also"] and in its line; the others become status='superseded' (superseded_by = the kept one)."""
    with session() as s:
        q = (select(Alert, AlertRule.kind).join(AlertRule, AlertRule.id == Alert.rule_id).where(Alert.status == "active"))
        if since:
            q = q.where(Alert.triggered_on >= since)
        groups = defaultdict(list)
        for a, kind in s.execute(q).all():
            groups[(a.company_id, a.triggered_on)].append((PRIORITY.get(kind, 9), a.id, a, kind))
        merged = superseded = 0
        for items in groups.values():
            if len(items) < 2:
                continue
            items.sort(key=lambda t: (t[0], t[1]))
            keep, rest = items[0][2], [t for t in items[1:]]
            detail = dict(keep.detail or {})
            also = list(detail.get("also") or [])
            for _, _, a, kind in rest:
                also.append({"kind": kind, "title": _short(a.title), "line": a.line, "link": a.deep_link, "alert_id": a.id})
                a.status, a.superseded_by = "superseded", keep.id
                superseded += 1
            detail["also"] = also
            keep.detail = detail
            base_line = keep.line.split(" · also: ")[0]
            keep.line = (base_line + " · also: " + "; ".join(x["title"] for x in also))[:600]
            if any(not a.read for _, _, a, _ in rest):
                keep.read = False
            merged += 1
    return {"groups_merged": merged, "superseded": superseded}


def reevaluate() -> dict:
    """Apply the current rules to every stored alert, from scratch and repeatably: undo previous merges, restate
    volume alerts that still meet the volume rule with its numbers, supersede (or bring back) the ones that don't,
    then merge to one alert per company per day. Nothing is deleted."""
    rs = rules()
    r_vol = rs.get("volume_spike")
    out = {"volume_rewritten": 0, "volume_superseded": 0, "volume_restored": 0}
    with session() as s:                                     # 1. undo merges
        for a in s.scalars(select(Alert).where(Alert.superseded_by.is_not(None))):
            a.status, a.superseded_by = "active", None
        for a in s.scalars(select(Alert)):
            if (a.detail or {}).get("also"):
                a.detail = {k: v for k, v in a.detail.items() if k != "also"}
            if " · also: " in (a.line or ""):
                a.line = a.line.split(" · also: ")[0]
    if r_vol:                                                # 2. volume alerts against the current rule
        mult, min_val = float(r_vol.params.get("multiple", 3.0)), float(r_vol.params.get("min_avg_value_cr", 10.0))
        with session() as s:
            rows = s.execute(select(Price.company_id, Price.date, Price.close, Price.adj_close, Price.volume)
                             .where(Price.company_id.is_not(None)).order_by(Price.company_id, Price.date)).all()
        by_co = defaultdict(list)
        for cid, d, close, adj, vol in rows:
            by_co[cid].append({"date": d, "close": adj or close, "raw_close": close, "volume": vol})
        hits = {(cid, h["date"]): h for cid, prs in by_co.items() for h in volume_hits(prs, mult, date(2000, 1, 1), min_val)}
        reason = f"below the volume rule ({mult:.1f}× the 20-day average, ≥ ₹{min_val:,.0f} cr traded a day)"
        with session() as s:
            syms = dict(s.execute(select(Company.id, Company.nse_symbol)).all())
            for a in s.scalars(select(Alert).where(Alert.rule_id == r_vol.id)):
                h = hits.get((a.company_id, a.triggered_on))
                if h:
                    title, line = volume_text(syms.get(a.company_id, ""), h)
                    if a.title != title or a.line != line:
                        a.title, a.line = title, line
                        out["volume_rewritten"] += 1
                    if a.status != "active":
                        out["volume_restored"] += 1
                    a.status = "active"
                    a.detail = {k: v for k, v in {**(a.detail or {}), **h}.items() if k not in ("date", "superseded_reason", "median")}
                elif a.status == "active":
                    a.status = "superseded"
                    a.detail = {**(a.detail or {}), "superseded_reason": reason}
                    out["volume_superseded"] += 1
    out.update(consolidate(None))                            # 3. one per company per day
    with session() as s:
        from sqlalchemy import func
        week = date.today() - timedelta(days=7)
        active = select(func.count()).select_from(Alert).where(Alert.status == "active")
        out["active"] = s.scalar(active)
        out["unread_active"] = s.scalar(active.where(Alert.read.is_(False)))
        out["unread_last_7_days"] = s.scalar(active.where(Alert.read.is_(False), Alert.triggered_on >= week))
    return out


def evaluate(lookback_days: int = LOOKBACK_DAYS) -> dict:
    rs = rules()
    since = date.today() - timedelta(days=lookback_days)
    since_utc = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    out = {}
    for name, fn in [("price_volume", lambda: _price_alerts(rs.get("volume_spike"), rs.get("price_move"), since)),
                     ("announcement_type", lambda: _filing_alerts(rs["announcement_type"], since_utc) if "announcement_type" in rs else 0),
                     ("peer_read_across", lambda: _filing_alerts(rs["peer_read_across"], since_utc, peers=True) if "peer_read_across" in rs else 0),
                     ("new_promise", lambda: _promise_alerts(rs["new_promise"]) if "new_promise" in rs else 0)]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 - one rule never blocks the others
            log.exception("alert rule %s failed", name)
            out[name] = f"failed: {e}"[:200]
    try:
        out["consolidated"] = consolidate(since - timedelta(days=1))
    except Exception as e:  # noqa: BLE001
        log.exception("consolidate failed")
        out["consolidated"] = f"failed: {e}"[:200]
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)
    if "--reevaluate" in sys.argv:
        print(reevaluate())
    else:
        print(evaluate(int(sys.argv[1]) if len(sys.argv) > 1 else LOOKBACK_DAYS))
