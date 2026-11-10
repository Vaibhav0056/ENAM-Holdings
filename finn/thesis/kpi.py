"""Thesis Guardian maths: pure Python, unit-tested.

KPI values come from verified results cards (₹ crore tables): revenue growth YoY, EBITDA margin, PAT growth YoY,
PAT margin. Status rules (spec §5): a KPI breach -> watch; two consecutive breaches of one KPI, or a breaker event
-> at_risk; otherwise intact. Every status change carries its reason and the filing that triggered it.
"""
from __future__ import annotations

KPI_METRICS = {"revenue_growth_yoy", "ebitda_margin", "pat_growth_yoy", "pat_margin"}


def kpi_values(results_payload: dict) -> dict[str, float]:
    """From a results card payload: the four checkable KPIs, in %."""
    if not results_payload or not results_payload.get("ok"):
        return {}
    t = {r["metric"]: r for r in results_payload.get("table", []) if r.get("found")}
    out = {}
    rev = t.get("revenue", {}).get("current")
    if t.get("revenue", {}).get("yoy") is not None:
        out["revenue_growth_yoy"] = t["revenue"]["yoy"]
    if t.get("pat", {}).get("yoy") is not None:
        out["pat_growth_yoy"] = t["pat"]["yoy"]
    if rev:
        if t.get("ebitda", {}).get("current") is not None:
            out["ebitda_margin"] = round(100 * t["ebitda"]["current"] / rev, 1)
        if t.get("pat", {}).get("current") is not None:
            out["pat_margin"] = round(100 * t["pat"]["current"] / rev, 1)
    return out


def breached(kpi: dict, value: float) -> bool:
    th = float(kpi["threshold"])
    return value < th if kpi["op"] == ">=" else value > th


def check(kpis: list[dict], values: dict[str, float]) -> list[dict]:
    """One row per KPI that could be checked: {metric, op, threshold, value, breached}."""
    out = []
    for k in kpis:
        v = values.get(k["metric"])
        if v is None:
            continue
        out.append({"metric": k["metric"], "op": k["op"], "threshold": float(k["threshold"]), "value": v,
                    "breached": breached(k, v)})
    return out


def status(history: list[list[dict]], breaker_events: list[dict], watch_events: list[dict] | None = None) -> tuple[str, str]:
    """history: KPI checks per results period, oldest first. breaker_events: [{summary, filing_id}] (a thesis breaker
    or an integrity red flag). watch_events: things worth monitoring that don't break the thesis by themselves
    (a senior exit). Returns (status, reason)."""
    if breaker_events:
        e = breaker_events[-1]
        return "at_risk", f"breaker event: {e['summary']}"
    if len(history) >= 2:
        last, prev = history[-1], history[-2]
        prev_b = {c["metric"] for c in prev if c["breached"]}
        both = [c for c in last if c["breached"] and c["metric"] in prev_b]
        if both:
            c = both[0]
            return "at_risk", f"{c['metric']} breached two results in a row ({c['value']} vs {c['op']} {c['threshold']})"
    if history:
        b = [c for c in history[-1] if c["breached"]]
        if b:
            c = b[0]
            return "watch", f"{c['metric']} {c['value']} breaches {c['op']} {c['threshold']}"
    if watch_events:
        e = watch_events[-1]
        return "watch", e["summary"]
    if history and history[-1]:
        return "intact", "every checkable KPI within its threshold on the latest results"
    return "intact", "no KPI checkable yet and no breaker event"
