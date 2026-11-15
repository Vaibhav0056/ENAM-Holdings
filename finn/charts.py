"""Chart specs, computed in Python from verified database data only. The browser draws them (static/charts.js) and
never computes a number.

A spec is a plain dict:
    kind      bars | line | step | waterfall | stacked | heat | band | spark | price
    title     one line stating the finding (computed here, from the same numbers)
    unit      unit of the left axis ("₹ cr", "%", "×", "days", "₹", "per 1,000 words")
    x         category labels (quarters, calls, dates)
    series    [{"name", "values": [number | None], "as": "bar" | "line", "axis": "left" | "right", "unit"}]
              None is a gap: missing data is never interpolated or drawn as zero
    sources   optional, per series per point: {"title", "page", "document_id", "quote"} or None
    threshold optional {"value", "label"} (a band or a dashed line)
    events    optional [{"i", "label", "kind", "link"}] (price chart markers)
    grid      heat only: {"rows", "cols", "values"}
    summary   text for screen readers (aria-label)
    table     {"columns", "rows"} for "View as table"
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select

from . import fiscal
from .database import read_session_run
from .models import Company, Enrichment, Filing, Price

METRIC_NAME = {"revenue": "Revenue", "pat": "PAT", "ebitda": "EBITDA", "pbt": "PBT"}


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "Not in sources"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "₹ cr":
        return f"₹{v:,.0f} cr" if abs(v) >= 100 else f"₹{v:,.1f} cr"
    if unit == "×":
        return f"{v:.2f}×"
    return f"{v:,.2f}".rstrip("0").rstrip(".") + (f" {unit}" if unit else "")


def _pct(a, b):
    return None if a is None or b in (None, 0) else round((a / b - 1) * 100, 1)


def _table(x, series, unit_of=lambda s: s.get("unit", "")):
    return {"columns": [""] + [s["name"] for s in series],
            "rows": [[x[i]] + [_fmt(s["values"][i], unit_of(s)) for s in series] for i in range(len(x))]}


# ---------------------------------------------------------------- results history ------------------------------------

def quarter_history(company_id: int) -> dict[str, dict]:
    """quarter label -> {metric: value, "src": {metric: source}} from every verified results card of the company.
    Each card prints three quarters (current, previous, year ago); the newest card wins a quarter (restatements).
    Cards built from BSE's summary contribute revenue and PAT only, marked as BSE's."""
    def q(s):
        return [(f.id, f.filed_at, e.payload) for f, e in s.execute(
            select(Filing, Enrichment).join(Enrichment, Enrichment.filing_id == Filing.id)
            .where(Filing.company_id == company_id, Enrichment.type == "results", Filing.status != "duplicate")
            .order_by(Filing.filed_at)).all()]
    out: dict[str, dict] = {}
    for fid, filed, p in read_session_run(q):
        p = p or {}
        if not p.get("ok") or p.get("confidence") == "low" or (p.get("notes") or []):
            continue
        cur = fiscal.parse_period(p.get("period"))
        if not cur or not cur.q:
            continue
        cols = {"current": cur, "previous": fiscal.prev_quarter(cur), "year_ago": fiscal.year_ago(cur)}
        for row in p.get("table") or []:
            if not row.get("found"):
                continue
            for col, per in cols.items():
                v = row.get(col)
                if v is None:
                    continue
                slot = out.setdefault(per.label(), {"src": {}})
                slot[row["metric"]] = v
                slot["src"][row["metric"]] = None if p.get("from_bse_summary") else {
                    "title": f"Results filing ({p.get('period')}, {p.get('basis')})", "page": row.get("page"),
                    "document_id": p.get("document_id"), "quote": row.get("quote"), "filing_id": fid,
                    "derived": row.get("derived")}
                slot["basis"] = p.get("basis")
    return out


def _last_quarters(hist: dict, n: int = 8) -> list[str]:
    pers = sorted((fiscal.parse_period(k) for k in hist), key=lambda p: (p.fy, p.q))
    if not pers:
        return []
    last = pers[-1]
    labels = []
    p = last
    for _ in range(n):
        labels.append(p.label())
        p = fiscal.prev_quarter(p)
    return labels[::-1]


def results_chart(company_id: int, symbol: str, hist: dict | None = None) -> dict | None:
    hist = quarter_history(company_id) if hist is None else hist
    xs = _last_quarters(hist)
    if not xs:
        return None
    rev = [hist.get(x, {}).get("revenue") for x in xs]
    pat = [hist.get(x, {}).get("pat") for x in xs]
    margin = [round(100 * hist[x]["ebitda"] / hist[x]["revenue"], 1)
              if x in hist and hist[x].get("ebitda") is not None and hist[x].get("revenue") else None for x in xs]
    src = lambda m: [hist.get(x, {}).get("src", {}).get(m) for x in xs]  # noqa: E731
    have = sum(v is not None for v in rev)
    last = xs[-1]
    ya = fiscal.year_ago(fiscal.parse_period(last)).label()
    bits = []
    yoy = _pct(hist.get(last, {}).get("revenue"), hist.get(ya, {}).get("revenue"))
    if yoy is not None:
        bits.append(f"revenue {'up' if yoy >= 0 else 'down'} {abs(yoy):.1f}% YoY in {last}")
    if margin[-1] is not None:
        prev_m = margin[xs.index(ya)] if ya in xs else None
        bits.append(f"EBITDA margin {margin[-1]:.1f}%" + (f", from {prev_m:.1f}% a year ago" if prev_m is not None else ""))
    title = (symbol + ": " + "; ".join(bits)) if bits else f"{symbol}: quarterly revenue and PAT"
    series = [{"name": "Revenue", "values": rev, "as": "bar", "axis": "left", "unit": "₹ cr"},
              {"name": "PAT", "values": pat, "as": "bar", "axis": "left", "unit": "₹ cr"},
              {"name": "EBITDA margin", "values": margin, "as": "line", "axis": "right", "unit": "%"}]
    return {"id": f"results-{symbol}", "kind": "bars", "title": title, "unit": "₹ cr", "x": xs, "series": series,
            "sources": [src("revenue"), src("pat"), src("ebitda")],
            "note": f"{have} of {len(xs)} quarters in FINN's verified results cards; gaps are quarters it has no verified figures for."
                    + (" EBITDA is derived (PBT + finance costs + depreciation − other income) where not printed." if any(
                        (s or {}).get("derived") for s in src("ebitda")) else ""),
            "summary": f"{title}. " + "; ".join(f"{x}: revenue {_fmt(r, '₹ cr')}, PAT {_fmt(p, '₹ cr')}, margin {_fmt(m, '%')}"
                                                for x, r, p, m in zip(xs, rev, pat, margin)),
            "table": _table(xs, series)}


def margin_bridge(company_id: int, symbol: str) -> dict | None:
    """Latest verified quarter, EBITDA margin to PAT margin, each step as % of revenue (a waterfall)."""
    def q(s):
        return s.execute(select(Enrichment.payload, Filing.id).join(Filing, Filing.id == Enrichment.filing_id)
                         .where(Filing.company_id == company_id, Enrichment.type == "results", Filing.status != "duplicate")
                         .order_by(Filing.filed_at.desc())).all()
    for p, fid in read_session_run(q):
        p = p or {}
        lines = p.get("lines") or {}
        if not p.get("ok") or p.get("notes") or not lines.get("revenue") or p.get("from_bse_summary"):
            continue
        rev = lines["revenue"]
        need = ("pbt", "pat")
        if any(lines.get(k) is None for k in need):
            continue
        ebitda = lines.get("ebitda")
        if ebitda is None and all(lines.get(k) is not None for k in ("pbt", "finance_costs", "depreciation")):
            ebitda = lines["pbt"] + lines["finance_costs"] + lines["depreciation"] - (lines.get("other_income") or 0)
        if ebitda is None:
            continue
        pc = lambda v: round(100 * v / rev, 2)  # noqa: E731   (2 dp so the steps add up; labels show 1 dp)
        steps = [("EBITDA margin", pc(ebitda), "total")]
        if lines.get("depreciation") is not None:
            steps.append(("Depreciation", -pc(lines["depreciation"]), "delta"))
        if lines.get("finance_costs") is not None:
            steps.append(("Finance costs", -pc(lines["finance_costs"]), "delta"))
        if lines.get("other_income") is not None:
            steps.append(("Other income", pc(lines["other_income"]), "delta"))
        steps.append(("PBT margin", pc(lines["pbt"]), "total"))
        tax = lines["pbt"] - lines["pat"]
        steps.append(("Tax and other items", -pc(tax), "delta"))
        steps.append(("PAT margin", pc(lines["pat"]), "total"))
        xs = [s[0] for s in steps]
        vals = [s[1] for s in steps]
        short = {"EBITDA margin": "EBITDA", "Depreciation": "Depr.", "Finance costs": "Fin.", "Other income": "Oth.",
                 "PBT margin": "PBT", "Tax and other items": "Tax etc.", "PAT margin": "PAT"}
        title = (f"{symbol} {p.get('period')}: EBITDA margin {steps[0][1]:.1f}% becomes a PAT margin of {steps[-1][1]:.1f}%"
                 f"; the biggest step is {max((x for x in steps if x[2] == 'delta'), key=lambda x: abs(x[1]))[0].lower()}")
        return {"id": f"bridge-{symbol}", "kind": "waterfall", "title": title, "unit": "%", "x": xs,
                "axis": [short.get(x, x) for x in xs],
                "series": [{"name": "% of revenue", "values": vals, "as": "bar", "axis": "left", "unit": "%",
                            "steps": [s[2] for s in steps]}],
                "sources": [[{"title": f"Results filing ({p.get('period')}, {p.get('basis')})",
                              "document_id": p.get("document_id"), "page": next((r.get("page") for r in p.get("table", [])
                                                                                if r.get("found") and r.get("page")), None),
                              "quote": None, "filing_id": fid}] * len(xs)],
                "note": "Each step as a share of revenue for the quarter; 'Tax and other items' = PBT − PAT (tax, "
                        "exceptional items, share of associates).",
                "summary": f"{title}. " + "; ".join(f"{x} {v:+.1f}%" for x, v in zip(xs, vals)),
                "table": {"columns": ["Step", "% of revenue"], "rows": [[x, f"{v:+.2f}%"] for x, v in zip(xs, vals)]}}
    return None


# ---------------------------------------------------------------- holding --------------------------------------------

KPI_LABEL = {"revenue_growth_yoy": "Revenue growth YoY", "ebitda_margin": "EBITDA margin",
             "pat_growth_yoy": "PAT growth YoY", "pat_margin": "PAT margin"}


def kpi_charts(company_id: int, symbol: str, kpis: list[dict], hist: dict | None = None) -> list[dict]:
    """Each thesis KPI by quarter against its threshold (a band chart), from the verified quarter history."""
    hist = quarter_history(company_id) if hist is None else hist
    xs = _last_quarters(hist)
    out = []
    for k in kpis or []:
        vals = []
        for x in xs:
            h, ya = hist.get(x, {}), hist.get(fiscal.year_ago(fiscal.parse_period(x)).label(), {})
            m = k["metric"]
            if m == "ebitda_margin":
                v = round(100 * h["ebitda"] / h["revenue"], 1) if h.get("ebitda") is not None and h.get("revenue") else None
            elif m == "pat_margin":
                v = round(100 * h["pat"] / h["revenue"], 1) if h.get("pat") is not None and h.get("revenue") else None
            elif m == "revenue_growth_yoy":
                v = _pct(h.get("revenue"), ya.get("revenue"))
            elif m == "pat_growth_yoy":
                v = _pct(h.get("pat"), ya.get("pat"))
            else:
                v = None
            vals.append(v)
        th, op = float(k["threshold"]), k["op"]
        known = [(x, v) for x, v in zip(xs, vals) if v is not None]
        if not known:
            continue
        miss = [x for x, v in known if (v < th if op == ">=" else v > th)]
        label = KPI_LABEL.get(k["metric"], k["metric"])
        title = (f"{label} {'≥' if op == '>=' else '≤'} {th:g}%: " +
                 (f"held in all {len(known)} quarters FINN has" if not miss else
                  f"breached in {len(miss)} of {len(known)} quarters ({', '.join(miss[-3:])})"))
        series = [{"name": label, "values": vals, "as": "line", "axis": "left", "unit": "%"}]
        out.append({"id": f"kpi-{symbol}-{k['metric']}", "kind": "band", "title": title, "unit": "%", "x": xs,
                    "series": series, "threshold": {"value": th, "label": f"threshold {th:g}%", "op": op},
                    "summary": f"{title}. " + "; ".join(f"{x}: {_fmt(v, '%')}" for x, v in zip(xs, vals)),
                    "table": _table(xs, series)})
    return out


COMPANY_WIDE_EBITDA = __import__("re").compile(r"^(consolidated |company[- ]wide |overall |group )?ebitda margins?( guidance)?$", 2)


def guidance_band(company_id: int, symbol: str, hist: dict | None = None) -> dict | None:
    """When the thesis has no numeric KPI: company-wide EBITDA margin by quarter against management's own most recent
    company-wide EBITDA-margin target from the promise ledger (never a segment's target, never an operating-margin
    target, which is a different measure)."""
    from .models import Report

    def q(s):
        r = s.scalar(select(Report).where(Report.company_id == company_id, Report.kind == "core")
                     .order_by(Report.created_at.desc()).limit(1))
        return (r.data.get("ledger") or [], r.data.get("sources") or []) if r else ([], [])
    ledger, sources = read_session_run(q)
    cands = [p for p in ledger if COMPANY_WIDE_EBITDA.match((p.get("metric") or "").strip())
             and (p.get("unit") or "").strip() == "%" and p.get("target_low") is not None]
    if not cands:
        return None
    p = max(cands, key=lambda x: (fiscal.parse_period(x.get("said_in")) or fiscal.Period(0)).end())
    lo, hi = float(p["target_low"]), p.get("target_high")
    rng = f"{lo:g}–{float(hi):g}%" if hi not in (None, lo) else f"{lo:g}%"
    spec = kpi_charts(company_id, symbol, [{"metric": "ebitda_margin", "op": ">=", "threshold": lo}], hist)
    if not spec:
        return None
    spec = spec[0]
    doc = next((x for x in sources if x.get("id") == p.get("doc")), {})
    known = [(x, v) for x, v in zip(spec["x"], spec["series"][0]["values"]) if v is not None]
    below = [x for x, v in known if v < lo]
    spec.update(id=f"guidance-{symbol}",
                title=(f"EBITDA margin vs management's {rng} target (said {p.get('said_in')}): "
                       + (f"at or above {lo:g}% in all {len(known)} quarters FINN has" if not below else
                          f"below {lo:g}% in {len(below)} of {len(known)} quarters")),
                threshold={"value": lo, "label": f"target from {lo:g}%", "op": ">="},
                note=(f"Target from the promise ledger: \"{(p.get('quote') or '')[:160]}\" ({doc.get('title') or p.get('doc')}, "
                      f"page {p.get('page')}). Not your thesis: add your own numbers to watch with Edit thesis."),
                sources=[[{"title": doc.get("title") or "Earnings call", "page": p.get("page"),
                           "document_id": doc.get("document_id"), "quote": p.get("quote")}] * len(spec["x"])])
    spec["summary"] = spec["title"] + ". " + spec["summary"].split(". ", 1)[-1]
    return spec


def price_chart(company_id: int, symbol: str, days: int = 365) -> dict | None:
    """Daily close for a year with markers for results, management changes and governance filings."""
    since = date.today() - timedelta(days=days)

    def q(s):
        px = s.execute(select(Price.date, Price.adj_close, Price.close).where(Price.company_id == company_id,
                                                                            Price.date >= since).order_by(Price.date)).all()
        ev = s.execute(select(Filing.id, Filing.filed_at, Enrichment.type, Enrichment.headline)
                       .join(Enrichment, Enrichment.filing_id == Filing.id)
                       .where(Filing.company_id == company_id, Filing.status != "duplicate",
                              Enrichment.type.in_(["results", "mgmt_change", "governance"]),
                              Enrichment.bucket != "routine")
                       .order_by(Filing.filed_at)).all()
        return px, ev
    px, ev = read_session_run(q)
    px = [(d, a or c) for d, a, c in px if (a or c)]
    if len(px) < 2:
        return None
    xs = [d.isoformat() for d, _ in px]
    vals = [round(v, 2) for _, v in px]
    idx = {d: i for i, (d, _) in enumerate(px)}
    events, seen = [], set()
    for fid, filed, typ, head in ev:
        d = fiscal.to_ist(filed).date()
        if d < px[0][0]:
            continue
        i = idx.get(d) if d in idx else next((idx[x] for x in sorted(idx) if x >= d), None)
        if i is None or (i, typ) in seen:
            continue
        seen.add((i, typ))
        events.append({"i": i, "kind": {"results": "results", "mgmt_change": "mgmt", "governance": "governance"}[typ],
                       "label": (head or typ)[:90], "link": f"#card/{fid}", "date": d.isoformat()})
    chg = _pct(vals[-1], vals[0])
    title = (f"{symbol}: {'up' if chg >= 0 else 'down'} {abs(chg):.1f}% since {px[0][0]:%d %b %Y}"
             f"; {sum(e['kind'] == 'results' for e in events)} results, {sum(e['kind'] == 'mgmt' for e in events)} management"
             f" and {sum(e['kind'] == 'governance' for e in events)} governance filings marked")
    series = [{"name": "Close", "values": vals, "as": "line", "axis": "left", "unit": "₹"}]
    monthly = [i for i in range(len(xs)) if i == 0 or xs[i][:7] != xs[i - 1][:7]] + [len(xs) - 1]
    return {"id": f"price-{symbol}", "kind": "price", "title": title, "unit": "₹", "x": xs, "series": series,
            "events": events, "note": "Adjusted close (Yahoo Finance; NSE bhavcopy when Yahoo is unavailable).",
            "summary": f"{title}. From ₹{vals[0]:,.2f} to ₹{vals[-1]:,.2f}; high ₹{max(vals):,.2f}, low ₹{min(vals):,.2f}.",
            "table": {"columns": ["Date", "Close", "Filing"],
                      "rows": [[xs[i], f"₹{vals[i]:,.2f}", "; ".join(e["label"] for e in events if e["i"] == i)] for i in monthly]
                      + [["…", "", f"{len(xs)} trading days in all; first trading day of each month shown"]]}}


# ---------------------------------------------------------------- research -------------------------------------------

def hit_rate_chart(report: dict) -> dict | None:
    """Promises by the quarter they were made in, stacked by verdict."""
    ledger = report.get("ledger") or []
    if not ledger:
        return None
    by = defaultdict(lambda: defaultdict(int))
    for l in ledger:
        by[l.get("said_in") or "Undated"][l["verdict"]] += 1
    keys = sorted([k for k in by if fiscal.parse_period(k)], key=lambda k: (fiscal.parse_period(k).fy, fiscal.parse_period(k).q or 0))
    keys += [k for k in by if not fiscal.parse_period(k)]
    order = [("delivered", "Delivered"), ("partial", "Partial"), ("missed", "Missed"), ("pending", "Pending"),
             ("unverifiable", "Too vague")]
    series = [{"name": name, "values": [by[k][v] or 0 for k in keys], "as": "bar", "axis": "left", "unit": "",
               "verdict": v} for v, name in order]
    measured = sum(by[k][v] for k in keys for v in ("delivered", "partial", "missed"))
    delivered = sum(by[k]["delivered"] for k in keys)
    title = (f"{delivered} of {measured} measurable promises delivered" if measured else
             "No promise has reached its deadline yet") + f"; {sum(by[k]['pending'] for k in keys)} still pending"
    return {"id": f"hits-{report.get('symbol')}", "kind": "stacked", "title": title, "unit": "promises", "x": keys,
            "series": series, "summary": f"{title}. " + "; ".join(
                f"{k}: " + ", ".join(f"{by[k][v]} {name.lower()}" for v, name in order if by[k][v]) for k in keys),
            "table": {"columns": ["Said in"] + [n for _, n in order],
                      "rows": [[k] + [str(by[k][v]) for v, _ in order] for k in keys]}}


def drift_chart(report: dict) -> dict | None:
    for d in report.get("drift") or []:
        steps = d.get("steps") or []
        if steps and all(s.get("before_mid") is not None and s.get("after_mid") is not None for s in steps):
            xs = [steps[0]["from"]] + [s["to"] for s in steps]
            vals = [steps[0]["before_mid"]] + [s["after_mid"] for s in steps]
            unit = "₹ cr" if d.get("mid_unit") == "₹ crore" else (d.get("mid_unit") or "")
            chg = steps[-1]["change"]
            title = f"{d['metric']} ({d.get('horizon') or 'no horizon'}): guidance {chg} from {_fmt(vals[0], unit)} to {_fmt(vals[-1], unit)}"
            series = [{"name": d["metric"], "values": vals, "as": "line", "axis": "left", "unit": unit}]
            return {"id": f"drift-{report.get('symbol')}", "kind": "step", "title": title, "unit": unit, "x": xs,
                    "series": series, "note": "Mid-point of each stated range; money targets compared in ₹ crore.",
                    "summary": f"{title}. " + "; ".join(f"{x}: {_fmt(v, unit)}" for x, v in zip(xs, vals)),
                    "table": _table(xs, series)}
    return None


# ---------------------------------------------------------------- deep dives -----------------------------------------

def deep_charts(data: dict) -> list[dict]:
    r = data.get("recipe")
    out = []
    if r == "cash_quality":
        cur, prev = (data.get("inputs") or {}).get("current") or {}, (data.get("inputs") or {}).get("previous") or {}
        xs = ["Prior yr", "Latest yr"]
        ocf = [prev.get("ocf"), cur.get("ocf")]
        pat = [prev.get("pat"), cur.get("pat")]
        conv = [round(o / p, 2) if o is not None and p else None for o, p in zip(ocf, pat)]
        if any(v is not None for v in ocf + pat):
            last = conv[-1]
            if last is not None:
                title = f"Operating cash flow was {last:.2f}× PAT in {data.get('report')}"
            elif ocf[-1] is not None:
                chg = _pct(ocf[-1], ocf[0])
                title = (f"Operating cash flow {_fmt(ocf[-1], '₹ cr')} in {data.get('report')}"
                         + (f", {'up' if chg >= 0 else 'down'} {abs(chg):.1f}% on the prior year" if chg is not None else "")
                         + "; PAT not extracted, so no conversion ratio")
            else:
                title = "Operating cash flow: not in sources"
            ev = (data.get("evidence") or {})
            src = lambda k: [((ev.get("previous") or {}).get(k) or None), ((ev.get("current") or {}).get(k) or None)]  # noqa: E731
            cand = [({"name": "Operating cash flow", "values": ocf, "as": "bar", "axis": "left", "unit": "₹ cr"}, src("ocf")),
                    ({"name": "PAT", "values": pat, "as": "bar", "axis": "left", "unit": "₹ cr"}, src("pat")),
                    ({"name": "OCF / PAT", "values": conv, "as": "line", "axis": "right", "unit": "×"}, [None, None])]
            keep = [(sr, sc) for sr, sc in cand if any(v is not None for v in sr["values"])]   # no empty series
            series = [sr for sr, _ in keep]
            out.append({"id": "deep-cash", "kind": "bars", "title": title, "unit": "₹ cr", "x": xs, "series": series,
                        "sources": [[_deep_src(x, data) for x in sc] for _, sc in keep],
                        "summary": title + ". " + "; ".join(f"{x}: OCF {_fmt(o, '₹ cr')}, PAT {_fmt(p, '₹ cr')}"
                                                            for x, o, p in zip(xs, ocf, pat)),
                        "table": _table(xs, series)})
    if r == "balance_sheet":
        cur, prev = data.get("current") or {}, data.get("previous") or {}
        xs = ["Prior yr", "Latest yr"]
        nd = [prev.get("net_debt"), cur.get("net_debt")]
        wc = [prev.get("cash_conversion_cycle") if prev.get("cash_conversion_cycle") is not None else prev.get("working_capital_days"),
              cur.get("cash_conversion_cycle") if cur.get("cash_conversion_cycle") is not None else cur.get("working_capital_days")]
        if any(v is not None for v in nd):
            last = nd[-1]
            title = (f"Net cash of {_fmt(-last, '₹ cr')} in {data.get('report')}" if last is not None and last < 0 else
                     f"Net debt of {_fmt(last, '₹ cr')} in {data.get('report')}" if last is not None else "Net debt")
            series = [{"name": "Net debt (negative = net cash)", "values": nd, "as": "bar", "axis": "left", "unit": "₹ cr"}]
            if any(v is not None for v in wc):
                series.append({"name": "Working-capital days", "values": wc, "as": "line", "axis": "right", "unit": "days"})
            out.append({"id": "deep-bs", "kind": "bars", "title": title, "unit": "₹ cr", "x": xs, "series": series,
                        "summary": title + ". " + "; ".join(f"{x}: net debt {_fmt(v, '₹ cr')}" for x, v in zip(xs, nd)),
                        "table": _table(xs, series)})
    if r == "hedge_meter" and data.get("calls"):
        calls = data["calls"]
        xs = [c["call"] for c in calls]
        vals = [c.get("per_1000_words") for c in calls]
        chg = data.get("latest_vs_earlier_pct")
        title = (f"Cautious phrases on the latest call {'up' if chg >= 0 else 'down'} {abs(chg):.1f}% vs the earlier average"
                 if chg is not None else "Cautious phrases per 1,000 words, by call")
        series = [{"name": "Cautious phrases per 1,000 words", "values": vals, "as": "line", "axis": "left", "unit": ""}]
        out.append({"id": "deep-hedge", "kind": "spark", "title": title, "unit": "per 1,000 words", "x": xs,
                    "series": series, "sources": [[{"title": f"Earnings call {c['call']}", "document_id": c.get("document_id"),
                                                    "page": 1, "quote": None} for c in calls]],
                    "summary": title + ". " + "; ".join(f"{x}: {v}" for x, v in zip(xs, vals)), "table": _table(xs, series)})
    if r == "worry_map" and data.get("topics"):
        rows, cols = data["topics"][:8], data["quarters"]
        vals = [[(data["counts"].get(t) or {}).get(q) or 0 for q in cols] for t in rows]
        rising = data.get("rising") or []
        title = (f"Rising on the latest call: {', '.join(rising)}" if rising else
                 f"{data.get('questions')} analyst questions; no topic rising")
        out.append({"id": "deep-worry", "kind": "heat", "title": title, "unit": "questions", "x": cols,
                    "grid": {"rows": rows, "cols": cols, "values": vals, "highlight": rising}, "series": [],
                    "summary": title + ". " + "; ".join(f"{t}: " + ", ".join(f"{c} {v}" for c, v in zip(cols, row) if v)
                                                       for t, row in zip(rows, vals)),
                    "table": {"columns": ["Topic"] + cols, "rows": [[t] + [str(v) for v in row] for t, row in zip(rows, vals)]}})
    return out


def _deep_src(e: dict | None, data: dict) -> dict | None:
    if not e:
        return None
    return {"title": data.get("report") or "Annual report", "page": e.get("page"), "document_id": data.get("document_id"),
            "quote": e.get("quote")}


# ---------------------------------------------------------------- alerts ---------------------------------------------

def volume_chart(company_id: int, symbol: str, on: date, multiple: float) -> dict | None:
    """The 20 sessions before a volume alert and the alert day, with the rule's threshold line (multiple × the
    20-day average)."""
    def q(s):
        return s.execute(select(Price.date, Price.volume).where(Price.company_id == company_id, Price.date <= on)
                         .order_by(Price.date.desc()).limit(21)).all()
    rows = list(reversed(read_session_run(q)))
    if len(rows) < 21 or rows[-1][0] != on:
        return None
    base = [v for _, v in rows[:-1] if v]
    if len(base) < 20:
        return None
    avg = sum(base) / len(base)
    xs = [d.isoformat() for d, _ in rows]
    vals = [round(v / 1e5, 1) if v else None for _, v in rows]
    ratio = rows[-1][1] / avg
    title = f"{symbol} traded {ratio:.1f}× its 20-day average on {on:%d %b}; the alert fires at {multiple:.1f}×"
    series = [{"name": "Volume (lakh shares)", "values": vals, "as": "bar", "axis": "left", "unit": "lakh"}]
    return {"id": f"vol-{symbol}-{on}", "kind": "bars", "title": title, "unit": "lakh", "x": xs, "series": series,
            "threshold": {"value": round(multiple * avg / 1e5, 1), "label": f"{multiple:.1f}× average"},
            "summary": f"{title}. Average {avg / 1e5:,.1f} lakh shares; alert day {rows[-1][1] / 1e5:,.1f} lakh.",
            "table": _table(xs, series)}


# ---------------------------------------------------------------- bundles for the API --------------------------------

def company(symbol: str) -> Company | None:
    def q(s):
        c = s.scalar(select(Company).where(Company.nse_symbol == symbol.upper()))
        if c:
            s.expunge(c)
        return c
    return read_session_run(q)


_bundle_cache: dict[str, tuple[float, dict]] = {}
BUNDLE_TTL_S = 900


def invalidate() -> None:
    """Called at the end of every pipeline run: new filings or prices change the charts."""
    _bundle_cache.clear()


def holding_bundle(symbol: str) -> dict:
    import time as _t
    hit = _bundle_cache.get(symbol.upper())
    if hit and _t.time() - hit[0] < BUNDLE_TTL_S:
        return hit[1]
    out = _holding_bundle(symbol)
    if out:
        _bundle_cache[symbol.upper()] = (_t.time(), out)
    return out


def _holding_bundle(symbol: str) -> dict:
    from .models import Thesis
    c = company(symbol)
    if not c:
        return {}

    def kq(s):
        t = s.get(Thesis, c.id)
        return list(t.kpis or []) if t else []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:      # independent queries: run them together
        f_hist, f_kpis = ex.submit(quarter_history, c.id), ex.submit(read_session_run, kq)
        f_bridge, f_price = ex.submit(margin_bridge, c.id, c.nse_symbol), ex.submit(price_chart, c.id, c.nse_symbol)
        hist, kpis = f_hist.result(), f_kpis.result()
        bands = kpi_charts(c.id, c.nse_symbol, kpis, hist) if kpis else []
        if not bands:
            g = guidance_band(c.id, c.nse_symbol, hist)
            bands = [g] if g else []
        return {"results": results_chart(c.id, c.nse_symbol, hist), "bridge": f_bridge.result(),
                "kpis": bands, "price": f_price.result()}


def research_bundle(report: dict) -> dict:
    return {"hit_rate": hit_rate_chart(report), "drift": drift_chart(report)}
