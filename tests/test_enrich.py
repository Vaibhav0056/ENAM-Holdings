"""Phase 3: results maths and checks, people verification, governance flags, order sizing, headlines."""
import pytest

from finn.enrich import governance, mgmt, results
from finn.enrich.run import order_value_cr, people_headline, results_headline
from finn.feed import score as sc
from finn.sources.bse_snapshot import parse as parse_snapshot, period_label, ttm_revenue

ROWS = {  # ₹ lakh, as printed
    "revenue": {"current_quarter": 120000, "previous_quarter": 110000, "same_quarter_last_year": 100000, "page": 3, "quote": "Revenue from operations 120000 110000 100000"},
    "other_income": {"current_quarter": 2000, "previous_quarter": 1500, "same_quarter_last_year": 1000, "page": 3, "quote": "q"},
    "finance_costs": {"current_quarter": 500, "previous_quarter": 500, "same_quarter_last_year": 400, "page": 3, "quote": "q"},
    "depreciation": {"current_quarter": 3000, "previous_quarter": 2900, "same_quarter_last_year": 2500, "page": 3, "quote": "q"},
    "pbt": {"current_quarter": 20000, "previous_quarter": 18000, "same_quarter_last_year": 15000, "page": 3, "quote": "q"},
    "tax": {"current_quarter": 5000, "previous_quarter": 4500, "same_quarter_last_year": 3750, "page": 3, "quote": "q"},
    "pat": {"current_quarter": 15000, "previous_quarter": 13500, "same_quarter_last_year": 11250, "page": 3, "quote": "q"},
}


def row(out, m):
    return next(r for r in out["table"] if r["metric"] == m)


def test_build_normalises_to_crore_derives_ebitda_and_computes_changes():
    out = results.build(ROWS, "₹ in lakh", "consolidated", "test", "Q2 FY27")
    assert out["ok"] and out["unit"] == "₹ crore" and out["confidence"] == "high"
    assert row(out, "revenue")["current"] == 1200.0 and row(out, "revenue")["yoy"] == 20.0
    assert row(out, "revenue")["qoq"] == pytest.approx(9.1)
    e = row(out, "ebitda")
    assert e["current"] == pytest.approx(200 + 5 + 30 - 20) and e["derived"].startswith("PBT")
    assert out["pat_yoy"] == pytest.approx(33.3) and out["tax_gap"] == 0.0


def test_reported_ebitda_wins_over_derived():
    rows = dict(ROWS, ebitda={"current_quarter": 22000, "previous_quarter": 20000, "same_quarter_last_year": 17000, "page": 3, "quote": "EBITDA"})
    assert row(results.build(rows, "lakh", "consolidated", "t"), "ebitda").get("derived") is None


def test_tax_gap_lowers_confidence_and_big_swings_mark_low():
    rows = {k: dict(v) for k, v in ROWS.items()}
    rows["tax"]["current_quarter"] = 2000                       # PBT 200 - tax 20 != PAT 150
    assert results.build(rows, "lakh", "consolidated", "t")["confidence"] == "medium"
    rows = {k: dict(v) for k, v in ROWS.items()}
    rows["pat"]["same_quarter_last_year"] = 1000                 # +1400% YoY
    out = results.build(rows, "lakh", "consolidated", "t")
    assert out["confidence"] == "low" and any("±300%" in n for n in out["notes"])


def test_unknown_unit_is_refused_not_guessed():
    assert results.build(ROWS, "units", "consolidated", "t")["ok"] is False


def test_bse_check_matches_within_one_percent():
    snap = {"revenue": {"Q2 FY27": 1205.0}, "net_profit": {"Q2 FY27": 150.4}}
    out = results.build(ROWS, "lakh", "consolidated", "t", "Q2 FY27", snapshot=snap)
    assert out["bse_check"]["match"] is True
    snap["revenue"]["Q2 FY27"] = 1300.0
    assert results.build(ROWS, "lakh", "consolidated", "t", "Q2 FY27", snapshot=snap)["bse_check"]["match"] is False


@pytest.mark.parametrize("text,period", [
    ("Unaudited Financial Results For The Quarter And Half Year Ended 30 September 2026", "Q2 FY27"),
    ("Results- Quarterly- September 30, 2026", "Q2 FY27"),
    ("Financial results for the quarter ended December 31, 2026", "Q3 FY27"),
    ("Outcome of board meeting", None),
])
def test_period_from_text(text, period):
    assert results.period_from_text(text) == period


def test_snapshot_parsing():
    raw = {"results_in_crores": {"fields": ["title", "Sep-26", "Jun-26", "FY25-26"],
                                 "data": [["Revenue", "3,647.00", "2,974.00", "10,388.00"], ["Net Profit", "1,140.00", "891.00", "2,607.00"]]}}
    snap = parse_snapshot(raw)
    assert snap["revenue"] == {"Q2 FY27": 3647.0, "Q1 FY27": 2974.0, "FY26": 10388.0}
    assert ttm_revenue(snap) == 10388.0 and period_label("Mar-27") == "Q4 FY27"


@pytest.mark.parametrize("text,value", [("TCS wins Rs 122 crore bid", 122.0), ("orders worth ₹1,450 cr", 1450.0),
                                        ("order of INR 50 lakh", 0.5), ("L&T Wins Orders (Large*)", None)])
def test_order_value(text, value):
    assert order_value_cr(text) == value


def test_order_magnitude_scales_with_share_of_revenue():
    assert sc.order_magnitude(122, 255000)[0] == pytest.approx(0.254, abs=0.01)   # 0.05% of TCS revenue
    assert sc.order_magnitude(2600, 26000)[0] == 1.0                               # 10% of revenue
    assert sc.order_magnitude(None, 1000) is None


PAGES = ["Intimation of resignation\nMr. Rajesh Kumar, Chief Financial Officer of the Company, has resigned with effect "
         "from 30 November 2026 to pursue opportunities outside the Company.\nBrief profile: He is a Chartered "
         "Accountant with 22 years of experience in finance."]


def test_verify_person_requires_quote_on_page_and_drops_unverified_extras():
    p = {"filing_id": 1, "name": "Rajesh Kumar", "role": "Chief Financial Officer", "direction": "resigned",
         "quote": "Mr. Rajesh Kumar, Chief Financial Officer of the Company, has resigned", "page": 1,
         "reason_quote": "to pursue opportunities outside the Company", "profile_quote": "He holds a PhD from MIT."}
    v = mgmt.verify_person(dict(p), PAGES)
    assert v and v["senior"] and v["reason_quote"] and v["profile_quote"] is None
    assert mgmt.verify_person(dict(p, quote="Mr. Rajesh Kumar was appointed CEO"), PAGES) is None
    flags = mgmt.governance_flags([v])
    assert flags[0]["kind"] == "senior_exit" and flags[0]["severity"] == "high"


def test_fallback_names_people_without_a_model():
    people = mgmt.fallback("Change in Management", "", PAGES)
    assert people[0]["name"] == "Rajesh Kumar" and people[0]["direction"] == "resigned"


def test_governance_rule_flags():
    kinds = {f["kind"] for f in governance.rule_flags("governance", "Resignation of Statutory Auditor", "")}
    assert kinds == {"auditor_resignation"}
    assert governance.rule_flags("litigation_regulatory", "CIRP-Updates", "Order passed by NCLAT")[0]["kind"] == "insolvency"
    assert governance.rule_flags("other", "Resignation of Statutory Auditor", "") == []


def test_headlines_from_payloads():
    out = results.build(ROWS, "lakh", "consolidated", "t", "Q2 FY27")
    assert results_headline(out) == "Q2 FY27 results: Revenue ₹1,200 cr (+20.0% YoY), PAT ₹150 cr (+33.3% YoY)"
    assert people_headline([{"name": "Rajesh Kumar", "role": "CFO", "direction": "resigned"}]) == "Rajesh Kumar, CFO: resigns"


def test_power_of_ten_mismatch_with_bse_is_rejected_not_shown():
    snap = {"revenue": {"Q2 FY27": 120000.0}, "net_profit": {"Q2 FY27": 15000.0}}   # BSE says 100x larger
    out = results.build(ROWS, "lakh", "consolidated", "t", "Q2 FY27", snapshot=snap)
    assert out["ok"] is False and "100× off" in out["reason"]


def test_bse_agreement_overrides_parser_disagreement():
    snap = {"revenue": {"Q2 FY27": 1200.0}, "net_profit": {"Q2 FY27": 150.0}}
    out = results.build(ROWS, "lakh", "consolidated", "t", "Q2 FY27", snapshot=snap, cross={"revenue": 999.0, "pat": 150.0})
    assert out["confidence"] == "high" and any("BSE's figures confirm" in n for n in out["notes"])


def test_period_falls_back_to_filing_date():
    from datetime import date
    out = results.extract(["no statement here"], "Outcome of board meeting", filed_on=date(2026, 10, 29), use_llm=False)
    assert out["period"] == "Q2 FY27"
