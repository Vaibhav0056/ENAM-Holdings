"""Threshold alerts. All maths in Python; deduped per (rule, ticker, date)."""
import logging
from datetime import date, datetime, timedelta
from statistics import median
from . import db

log = logging.getLogger("finn.alerts")

DEFAULT_RULES = [
    ("volume_spike", "Volume ≥ N × 20-day median", 2.0, None),
    ("price_move", "Daily move ≥ N % (or unusual for the stock)", 5.0, None),
    ("announcement_type", "Material filing types", None, "results,mgmt_change,m_and_a"),
]
LOOKBACK_DAYS = 10
VOL_WINDOW = 20        # sessions in the volume baseline
SIGMA_WINDOW = 60      # sessions of returns behind a stock's "normal" daily move
SIGMA_MIN_OBS = 20
SIGMA_MULT = 3.0       # a move this many sigmas beyond the market is unusual for the stock...
SIGMA_FLOOR = 0.5      # ...provided it is also at least this fraction of the fixed % threshold
VOL_COOLDOWN = 3       # sessions; inside one burst of heavy volume only an escalation re-alerts
MIN_BREADTH = 5        # symbols needed before a cross-sectional median counts as "the market"


def seed_rules():
    for kind, label, th, types in DEFAULT_RULES:
        db.x("INSERT INTO alert_rules(kind,label,threshold,types,enabled) VALUES(?,?,?,?,1) "
             "ON CONFLICT(kind) DO UPDATE SET label=excluded.label", (kind, label, th, types))


def rules() -> dict:
    seed_rules()
    return {r["kind"]: r for r in db.q("SELECT * FROM alert_rules")}


# ---------- pure maths (tested) ----------

def volume_baseline(volumes: list[float], window: int = VOL_WINDOW):
    """Median of the `window` traded sessions before the last one. Median, not mean: a single earlier
    spike (a results day) would otherwise inflate the baseline for 20 sessions and mask the next one."""
    base = [v for v in volumes[:-1] if v and v > 0][-window:]
    return median(base) if len(base) == window else None


def volume_ratio(volumes: list[float], window: int = VOL_WINDOW):
    """Ratio of the last volume to its baseline (see volume_baseline)."""
    base = volume_baseline(volumes, window)
    return None if base is None else volumes[-1] / base


def daily_move_pct(prev_close, close):
    if not prev_close:
        return None
    return (close - prev_close) / prev_close * 100.0


def return_sigma(closes: list[float], window: int = SIGMA_WINDOW, min_obs: int = SIGMA_MIN_OBS):
    """The stock's normal daily move in %, from returns BEFORE the last close. Robust (1.4826 x MAD), so
    past jump days don't widen the band. None when history is short or flat."""
    prior = closes[:-1][-(window + 1):]
    rets = [m for m in (daily_move_pct(a, b) for a, b in zip(prior, prior[1:])) if m is not None]
    if len(rets) < min_obs:
        return None
    mid = median(rets)
    return 1.4826 * median(abs(r - mid) for r in rets) or None


def market_moves(rows_by_symbol: dict[str, list[dict]]) -> dict[str, float]:
    """date -> median daily move across the portfolio: the market's part of any one stock's move."""
    by_date = {}
    for rows in rows_by_symbol.values():
        for prev, cur in zip(rows, rows[1:]):
            mv = daily_move_pct(prev["close"], cur["close"])
            if mv is not None:
                by_date.setdefault(cur["date"], []).append(mv)
    return {d: median(v) for d, v in by_date.items() if len(v) >= MIN_BREADTH}


def price_hits(rows: list[dict], vol_mult: float, move_pct: float, since: str, market: dict | None = None):
    """rows: ascending price rows for one symbol. Yields (kind, date, title_suffix, body).

    A price move fires on the fixed rule (|move| >= move_pct) or when it is unusual for THIS stock:
    its move in excess of the market is >= SIGMA_MULT sigmas of its own history. 3% is noise for a
    small-cap and an event for a large bank; a market-wide selloff is not sixteen stock events."""
    vols = [r["volume"] for r in rows]
    closes = [r["close"] for r in rows]
    spikes = {}  # row index -> volume ratio, for every session at or above vol_mult
    for i in range(1, len(rows)):
        d = rows[i]["date"]
        base = volume_baseline(vols[: i + 1]) if vol_mult is not None else None
        if base and vols[i] / base >= vol_mult:
            spikes[i] = vols[i] / base
        if d < since:
            continue
        if i in spikes and spikes[i] > max(spikes.get(j, 0) for j in range(i - VOL_COOLDOWN, i)):
            yield ("volume_spike", d, f"volume {spikes[i]:.1f}× 20-day median",
                   f"{vols[i] / 1e5:,.1f} lakh shares vs median {base / 1e5:,.1f} lakh")
        if move_pct is not None:
            mv = daily_move_pct(closes[i - 1], closes[i])
            if mv is None:
                continue
            mkt = (market or {}).get(d)
            sigma = return_sigma(closes[: i + 1])
            z = abs(mv - (mkt or 0.0)) / sigma if sigma else None
            unusual = z is not None and z >= SIGMA_MULT and abs(mv) >= SIGMA_FLOOR * move_pct
            if abs(mv) >= move_pct or unusual:
                context = [f"Closed ₹{closes[i]:,.2f} from ₹{closes[i - 1]:,.2f}"]
                if sigma:
                    context.append(f"normal day ±{sigma:.1f}%")
                if mkt is not None:
                    context.append(f"portfolio median {mkt:+.1f}%")
                yield ("price_move", d, f"{'up' if mv > 0 else 'down'} {abs(mv):.1f}% today" + (f" ({z:.1f}σ)" if z and z >= SIGMA_MULT else ""),
                       " · ".join(context))


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
    prices = {s["nse_symbol"]: db.q("SELECT * FROM prices WHERE symbol=? ORDER BY date", (s["nse_symbol"],))
              for s in db.portfolio()}
    market = market_moves(prices)
    for sym, rows in prices.items():
        try:
            for kind, d, suffix, body in price_hits(rows, vol_mult, move, since, market):
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
