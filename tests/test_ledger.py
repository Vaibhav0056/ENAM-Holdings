"""Phase 4 pure logic: matching, verdicts, dedupe, credibility, drift, prose guards."""
import pytest

from finn import guards
from finn.research import ledger as L


def P(metric, lo, hi, unit, horizon, said, pid="G1", text=None):
    return {"id": pid, "metric": metric, "target_low": lo, "target_high": hi, "unit": unit, "horizon": horizon,
            "said_in": said, "target_text": text or f"{lo}-{hi}{unit}"}


def A(metric, value, unit, per, aid="A1"):
    return {"id": aid, "metric": metric, "value": value, "unit": unit, "period": per}


@pytest.mark.parametrize("a,b,ok", [("EBITDA margin", "EBITDA margins", True), ("Revenue growth", "revenue growth YoY", True),
                                    ("FMEG EBITDA margin", "EBITDA margin", False), ("capital expenditure", "Capex", True),
                                    ("export revenue", "revenue", False)])
def test_metric_match(a, b, ok):
    assert L.metric_match(a, b) is ok


def test_verdicts():
    latest = L.period("Q2 FY27")
    p = P("revenue growth", 15, 17, "%", "FY26", "Q1 FY26")
    assert L.verdict_for(p, A("revenue growth", 16.2, "%", "FY26"), latest) == "delivered"
    assert L.verdict_for(p, A("revenue growth", 14.0, "%", "FY26"), latest) == "partial"
    assert L.verdict_for(p, A("revenue growth", 11.0, "%", "FY26"), latest) == "missed"
    assert L.verdict_for(p, A("revenue growth", 16.0, "crore", "FY26"), latest) == "unverifiable"
    assert L.verdict_for(P("revenue growth", None, None, "%", "FY26", "Q1 FY26"), None, latest) == "unverifiable"
    assert L.verdict_for(P("revenue growth", 15, 17, "%", "FY28", "Q1 FY27"), None, latest) == "pending"
    assert L.verdict_for(p, None, latest) == "unverifiable"            # horizon passed, nothing reported
    debt = P("net debt", 1000, 1000, "crore", "FY26", "Q1 FY26")
    assert L.verdict_for(debt, A("net debt", 950, "crore", "FY26"), latest) == "delivered"
    assert L.verdict_for(debt, A("net debt", 1200, "crore", "FY26"), latest) == "missed"


def test_match_requires_the_promised_period_and_tracks_ratios_only():
    promises = [P("EBITDA margin", 12, 13, "%", "FY27", "Q1 FY27", "G1"), P("capex", 1000, 1000, "crore", "FY27", "Q1 FY27", "G2")]
    actuals = [A("EBITDA margin", 12.6, "%", "Q2 FY27", "A1"), A("capex", 300, "crore", "Q2 FY27", "A2")]
    rows = {r["promise"]["id"]: r for r in L.match(promises, actuals)}
    assert rows["G1"]["verdict"] == "pending" and rows["G1"]["tracking"] == "on track"
    assert rows["G2"]["verdict"] == "pending" and rows["G2"]["tracking"] is None   # a quarter's capex vs a year's plan


def test_dedupe_counts_a_repeated_promise_once():
    ps = [P("revenue growth", 15, 17, "%", "FY27", "Q4 FY26", "G1"), P("Revenue growth", 15, 17, "%", "FY27", "Q1 FY27", "G2"),
          P("revenue growth", 12, 14, "%", "FY27", "Q2 FY27", "G3")]
    out = L.dedupe(ps)
    assert len(out) == 2 and next(p for p in out if p["target_low"] == 15)["also_said_in"] == ["Q4 FY26"]


def test_credibility_is_recency_weighted_and_needs_three():
    def row(v, said):
        return {"verdict": v, "promise": {"said_in": said}}
    rows = [row("delivered", "Q2 FY27"), row("missed", "Q1 FY27"), row("partial", "Q4 FY26"), row("pending", "Q2 FY27"),
            row("unverifiable", "Q2 FY27")]
    c = L.credibility(rows)
    expected = (1 * 1 + 0.8 * 0 + 0.64 * 0.5) / (1 + 0.8 + 0.64)
    assert c["score"] == round(100 * expected) and c["measured"] == 3 and c["vagueness_pct"] == 20
    assert L.credibility(rows[:2])["score"] is None


def test_drift_cut_raised_and_dropped():
    ps = [P("revenue growth", 15, 17, "%", "FY27", "Q4 FY26", "G1", "15-17%"),
          P("revenue growth", 12, 14, "%", "FY27", "Q1 FY27", "G2", "12-14%"),
          P("EBITDA margin", 11, 12, "%", "FY27", "Q4 FY26", "G3"),
          P("EBITDA margin", 11, 12, "%", "FY27", "Q1 FY27", "G6"),   # repeated twice, then silent on Q2 FY27
          P("export share", 10, 10, "%", "FY27", "Q4 FY26", "G7"),    # said once: never "dropped"
          P("store additions", 50, 50, "stores", "FY28", "Q1 FY27", "G4")]
    ps.append(P("store additions", 60, 60, "stores", "FY28", "Q2 FY27", "G5"))
    d = {x["metric"]: x for x in L.drift(ps)}
    assert d["revenue growth"]["steps"][0]["change"] == "cut"
    assert d["store additions"]["steps"][0]["change"] == "raised"
    assert d["EBITDA margin"]["dropped"] is True and d["EBITDA margin"]["steps"][0]["change"] == "maintained"
    assert "export share" not in d


def test_guards_remove_unsupported_numbers_and_advice():
    allowed = guards.allowed_numbers({"margin": 14.4, "growth": 12.35})
    text = "Margins reached 14.4% in Q2 FY27. Growth was 12.4%. Revenue rose 18%. Investors should buy the stock."
    clean, bad_num, bad_adv = guards.scrub(text, allowed)
    assert clean == "Margins reached 14.4% in Q2 FY27. Growth was 12.4%."
    assert (bad_num, bad_adv) == (1, 1)
    assert guards.scrub("Target price is 2,000.", {2000.0})[2] == 1


@pytest.mark.parametrize("text,advice", [("Investors should buy on dips.", True), ("We maintain a BUY rating.", True),
                                         ("The stock looks undervalued.", True), ("Fair value of 2,400.", True),
                                         ("The company sells cables and wires.", False), ("Promoters hold 50% of shares.", False)])
def test_advice_guard_targets_phrasing_not_words(text, advice):
    assert bool(guards.ADVICE.search(text)) is advice


def test_money_units_are_compared_in_crore():
    latest = L.period("Q2 FY27")
    p = P("capex", 1200, 1600, "crore", "FY26", "Q2 FY26")
    assert L.verdict_for(p, A("capital expenditure", 14.2, "INR billion", "FY26"), latest) == "delivered"
    assert L.verdict_for(p, A("capex", 9000, "million", "FY26"), latest) == "missed"
    assert L.unit_key("₹ million") == L.unit_key("crore") == "crore" and L.unit_key("%") == "%"


def test_share_of_revenue_never_matches_a_growth_rate():
    assert not L.metric_match("exports revenue contribution", "export revenue growth")
    assert not L.metric_match("EPC business contribution to revenue", "EPC revenue growth")
    assert L.metric_match("export share of revenue", "export contribution to revenue")
