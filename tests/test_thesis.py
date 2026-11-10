"""Phase 5: KPI values from results cards, breach checks, status rules, thesis multiplier, segment guard."""
from finn.thesis import guardian as G
from finn.thesis import kpi as K

CARD = {"ok": True, "table": [
    {"metric": "revenue", "found": True, "current": 1000.0, "yoy": 12.0},
    {"metric": "ebitda", "found": True, "current": 150.0, "yoy": 10.0},
    {"metric": "pbt", "found": True, "current": 120.0, "yoy": 9.0},
    {"metric": "pat", "found": True, "current": 90.0, "yoy": -5.0}]}
KPIS = [{"metric": "revenue_growth_yoy", "op": ">=", "threshold": 15}, {"metric": "ebitda_margin", "op": ">=", "threshold": 14},
        {"metric": "pat_margin", "op": "<=", "threshold": 20}]


def test_kpi_values_and_checks():
    v = K.kpi_values(CARD)
    assert v == {"revenue_growth_yoy": 12.0, "pat_growth_yoy": -5.0, "ebitda_margin": 15.0, "pat_margin": 9.0}
    checks = {c["metric"]: c["breached"] for c in K.check(KPIS, v)}
    assert checks == {"revenue_growth_yoy": True, "ebitda_margin": False, "pat_margin": False}
    assert K.kpi_values({"ok": False}) == {}


def test_status_rules():
    once = K.check(KPIS, K.kpi_values(CARD))
    fine = K.check(KPIS[1:], K.kpi_values(CARD))
    assert K.status([], [])[0] == "intact"
    assert K.status([fine], [])[0] == "intact"
    assert K.status([once], [])[0] == "watch"
    st, why = K.status([once, once], [])
    assert st == "at_risk" and "two results in a row" in why
    assert K.status([fine], [{"summary": "auditor resigned", "filing_id": 9}]) == ("at_risk", "breaker event: auditor resigned")


def test_multiplier_only_when_the_card_touches_the_thesis():
    assert G.multiplier("threatens") == (1.3, "touches your thesis (threatens)") and G.multiplier("supports") is None
    assert G.multiplier("none") is None and G.multiplier(None) is None


def test_segment_guard_keeps_company_wide_kpis():
    assert G._names_segment("Wires and Cables EBITDA margins W&C 11-13%", "W&C margin")
    assert G._names_segment("FMEG EBITDA margin 8-10%", "FMEG margin")
    assert not G._names_segment("EBITDA margin 11-13% (long term)", "company EBITDA margin")
    assert not G._names_segment("consolidated EBITDA margin 18%", "EBITDA margin")


def test_senior_exit_is_watch_not_at_risk():
    st, why = K.status([], [], [{"summary": "senior exit: CMD retired", "filing_id": 3}])
    assert st == "watch" and "CMD" in why
    assert K.status([], [{"summary": "auditor resigned", "filing_id": 4}], [{"summary": "x", "filing_id": 3}])[0] == "at_risk"
