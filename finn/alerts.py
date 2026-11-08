"""Threshold alerts. All maths in Python; deduped per (rule, ticker, date)."""
import logging
from datetime import date, datetime, timedelta
from . import db

log = logging.getLogger("finn.alerts")

DEFAULT_RULES = [
    ("volume_spike", "Volume ≥ N × 20-day average", 2.0, None),
    ("price_move", "Daily move ≥ N %", 5.0, None),
    ("announcement_type", "Material filing types", None, "results,mgmt_change,m_and_a"),
]
LOOKBACK_DAYS = 10


def seed_rules():
    for kind, label, th, types in DEFAULT_RULES:
        db.x("INSERT OR IGNORE INTO alert_rules(kind,label,threshold,types,enabled) VALUES(?,?,?,?,1)", (kind, label, th, types))


def rules() -> dict:
    seed_rules()
    return {r["kind"]: r for r in db.q("SELECT * FROM alert_rules")}


# ---------- pure maths (tested) ----------

def volume_ratio(volumes: list[float], window: int = 20):
    """Ratio of the last volume to the mean of the `window` volumes before it."""
    if len(volumes) < window + 1:
        return None
    base = volumes[-window - 1:-1]
    avg = sum(base) / window
    return None if avg <= 0 else volumes[-1] / avg


def daily_move_pct(prev_close, close):
    if not prev_close:
        return None
    return (close - prev_close) / prev_close * 100.0


def price_hits(rows: list[dict], vol_mult: float, move_pct: float, since: str):
    """rows: ascending price rows for one symbol. Yields (kind, date, title_suffix, body)."""
    vols = [r["volume"] for r in rows]
    for i in range(1, len(rows)):
        d = rows[i]["date"]
        if d < since:
            continue
        if vol_mult is not None:
            ratio = volume_ratio(vols[: i + 1])
            if ratio is not None and ratio >= vol_mult:
                yield ("volume_spike", d, f"volume {ratio:.1f}× 20-day avg",
                       f"{rows[i]['volume'] / 1e5:,.1f} lakh shares vs avg {sum(vols[i - 20:i]) / 20 / 1e5:,.1f} lakh")
        if move_pct is not None:
            mv = daily_move_pct(rows[i - 1]["close"], rows[i]["close"])
            if mv is not None and abs(mv) >= move_pct:
                yield ("price_move", d, f"{'up' if mv > 0 else 'down'} {abs(mv):.1f}% today",
                       f"Closed ₹{rows[i]['close']:,.2f} from ₹{rows[i - 1]['close']:,.2f}")


def _insert(kind, sym, d, title, body, link, ann_id=None) -> bool:
    before = db.q("SELECT id FROM alerts WHERE rule_kind=? AND symbol=? AND date=?", (kind, sym, d), one=True)
    if before:
        return False
    db.x("INSERT OR IGNORE INTO alerts(rule_kind,symbol,date,title,body,link,announcement_id) VALUES(?,?,?,?,?,?,?)",
         (kind, sym, d, title, body, link, ann_id))
    return True


TYPE_LABEL = {"results": "Results", "mgmt_change": "Management change", "m_and_a": "M&A"}


def alert_body(a: dict) -> str:
    """One line that says what happened: results deltas, the person + role, or the one-liner."""
    import json
    try:
        e = json.loads(a["enrichment_json"]) if a.get("enrichment_json") else {}
        if e.get("kind") == "results" and e.get("ok"):
            parts = []
            for t in e["table"]:
                if t["metric"] in ("revenue", "pat") and t.get("yoy") is not None:
                    parts.append(f"{'Revenue' if t['metric'] == 'revenue' else 'PAT'} {t['yoy']:+.1f}% YoY")
            if parts:
                return " · ".join(parts)
        if e.get("kind") == "mgmt_change" and e.get("people"):
            p = e["people"][0]
            return f"{p['name']} {p['direction']} as {p['role']}"[:120]
        if a.get("one_liner"):
            return json.loads(a["one_liner"])["text"]
    except Exception:
        pass
    return (a.get("subject") or "")[:110]


def evaluate(today: date | None = None) -> int:
    rs = rules()
    since = ((today or date.today()) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    new = 0
    vr, pr, ar = rs.get("volume_spike"), rs.get("price_move"), rs.get("announcement_type")
    vol_mult = vr["threshold"] if vr and vr["enabled"] else None
    move = pr["threshold"] if pr and pr["enabled"] else None
    for s in db.portfolio():
        sym = s["nse_symbol"]
        try:
            rows = db.q("SELECT * FROM prices WHERE symbol=? ORDER BY date", (sym,))
            for kind, d, suffix, body in price_hits(rows, vol_mult, move, since):
                new += _insert(kind, sym, d, f"{sym} {suffix}", body, f"/#research/{sym}")
        except Exception as e:
            log.warning("price alerts failed %s: %s", sym, e)
    if ar and ar["enabled"]:
        types = [t.strip() for t in (ar["types"] or "").split(",") if t.strip()]
        if types:
            marks = ",".join("?" * len(types))
            anns = db.q(f"SELECT * FROM announcements WHERE ann_type IN ({marks}) AND bucket<>'routine' AND status='ok' "
                        f"AND filed_at>=? ORDER BY score DESC", (*types, since))
            for a in anns:
                d = a["filed_at"][:10]
                ol = alert_body(a)
                new += _insert(f"type:{a['ann_type']}", a["symbol"], d,
                               f"{a['symbol']}: {TYPE_LABEL.get(a['ann_type'], a['ann_type'])}", ol, f"/#card/{a['id']}", a["id"])
    log.info("alerts: %s new", new)
    return new


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(evaluate())
