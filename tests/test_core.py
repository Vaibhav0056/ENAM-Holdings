"""Core maths and guards: QoQ/YoY, unit normalisation, scoring buckets, alert thresholds, quote verification,
period parsing / guidance matching, and the prose number scrubber."""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["FINN_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

import pytest
from finn import alerts, grounding, pull, push, results


# ---------- QoQ / YoY ----------

def test_pct_change_basic():
    assert results.pct_change(120, 100) == 20.0
    assert results.pct_change(80, 100) == -20.0


def test_pct_change_negative_base_uses_abs():
    # loss of -50 improving to profit of 50 is +200%, not -200%
    assert results.pct_change(50, -50) == 200.0


def test_pct_change_missing_or_zero_base():
    assert results.pct_change(10, 0) is None
    assert results.pct_change(None, 5) is None
    assert results.pct_change(5, None) is None


def test_ebitda_derivation():
    # EBITDA = PBT + finance costs + depreciation - other income
    assert results.ebitda_from(1000, 50, 200, 100) == 1150
    assert results.ebitda_from(1000, None, 200, 100) is None
    assert results.ebitda_from(1000, 50, 200, None) == 1250


# ---------- unit normalisation ----------

@pytest.mark.parametrize("value,unit,expected", [
    (98989.01, "million", 9898.901),
    (98989.01, "₹ million", 9898.901),
    (332861, "crore", 332861),
    (1234567, "lakh", 12345.67),
    (1234567, "Lacs", 12345.67),
    (5.2, "billion", 520.0),
])
def test_to_crore(value, unit, expected):
    assert results.to_crore(value, unit) == pytest.approx(expected)


def test_to_crore_unknown_unit_raises():
    with pytest.raises(ValueError):
        results.to_crore(10, "furlongs")


@pytest.mark.parametrize("text,unit", [
    ("CONSOLIDATED FINANCIAL RESULTS\n(₹ million)\nQuarter ended", "million"),
    ("Statement\n(f in Crores)\nParticulars", "crore"),
    ("{t in crore, except per share data and ratios)", "crore"),
    ("Rs. in Lakhs", "lakh"),
])
def test_detect_unit(text, unit):
    assert results.detect_unit(text) == unit


def test_parse_number_brackets_and_commas():
    assert results.parse_number("1,81,086.33") == 181086.33
    assert results.parse_number("(5,220.57)") == -5220.57
    assert results.parse_number("-") == 0.0
    assert results.parse_number("(3+4)") is None


def test_parse_page_skips_row_refs_and_note_numbers():
    page = ("CONSOLIDATED RESULTS (₹ crore)\n"
            "1 Revenue from operations 1 30578.40 21255.86 29523.30 60101.70\n"
            "5) Profit before tax(3+4) 13,062.20 10,582.23 9,210.01\n"
            "7) Profit for the period (5-6) 9,899.97 7,966.54 6,929.59 17,866.51\n")
    m = results.parse_page(page)
    assert m["revenue"]["current_quarter"] == 30578.40
    assert m["pbt"]["previous_quarter"] == 10582.23
    assert m["pat"]["current_quarter"] == 9899.97


def test_build_flags_absurd_change_as_low_confidence():
    metrics = {"revenue": {"current_quarter": 100, "previous_quarter": 90, "same_quarter_last_year": 80},
               "pat": {"current_quarter": 50, "previous_quarter": 5, "same_quarter_last_year": 10}}
    out = results.build(metrics, "crore", "consolidated", "parser")
    assert out["confidence"] == "low"
    pat = next(t for t in out["table"] if t["metric"] == "pat")
    assert pat["qoq"] == 900.0


def test_build_normalises_to_crore_and_computes_changes():
    metrics = {"revenue": {"current_quarter": 98989.01, "previous_quarter": 82097.32, "same_quarter_last_year": 64772.11},
               "pat": {"current_quarter": 9899.97, "previous_quarter": 7966.54, "same_quarter_last_year": 6929.59}}
    out = results.build(metrics, "million", "consolidated", "parser")
    rev = next(t for t in out["table"] if t["metric"] == "revenue")
    assert rev["current"] == pytest.approx(9898.901)
    assert rev["qoq"] == 20.6 and rev["yoy"] == 52.8
    assert out["confidence"] == "high"


# ---------- scoring ----------

def test_score_and_buckets():
    assert push.bucket_for(push.score_for("results", 1.0, 1.0)) == "attention"
    assert push.bucket_for(push.score_for("dividend_action", 1.0, 1.0)) == "worth_a_look"
    assert push.bucket_for(push.score_for("investor_meet", 1.0, 1.0)) == "routine"
    assert push.bucket_for(push.score_for("routine_compliance", 1.0, 1.0)) == "routine"
    # duplicate results filing gets low novelty and drops out of attention
    assert push.bucket_for(push.score_for("results", 1.0, 0.25)) == "routine"


def test_senior_mgmt_change_bumped():
    assert push.score_for("mgmt_change", 1.0, 1.0, senior=False) == 0.9
    assert push.score_for("mgmt_change", 1.0, 1.0, senior=True) == 1.0


def test_bucket_boundaries():
    assert push.bucket_for(0.7) == "attention"
    assert push.bucket_for(0.6999) == "worth_a_look"
    assert push.bucket_for(0.3) == "worth_a_look"
    assert push.bucket_for(0.2999) == "routine"


@pytest.mark.parametrize("cat,sub,subj,expected", [
    ("Result", "Financial Results", "Unaudited Financial Results for Q2", "results"),
    ("Company Update", "Analyst / Investor Meet", "Analyst meet intimation", "investor_meet"),
    ("Company Update", "Change in Management", "Change in Management", "mgmt_change"),
    ("Company Update", "", "Bagging/receiving of order(s) or contract(s)", "order_win"),
    ("Company Update", "Credit Rating", "Credit Rating", "credit_rating"),
    ("Company Update", "Newspaper Publication", "Newspaper clipping", "routine_compliance"),
    ("Board Meeting", "Board Meeting", "Board Meeting Intimation to consider results", "board_meeting_notice"),
    ("Company Update", "Acquisition", "Announcement under Regulation 30 (LODR)-Acquisition", "m_and_a"),
    ("Company Update", "Press Release / Media Release", "Press Release", None),
])
def test_rule_classify(cat, sub, subj, expected):
    assert push.rule_classify(cat, sub, subj) == expected


# ---------- alert thresholds ----------

def test_volume_ratio():
    vols = [100.0] * 20 + [250.0]
    assert alerts.volume_ratio(vols) == 2.5
    assert alerts.volume_ratio([100.0] * 10) is None


def test_daily_move():
    assert alerts.daily_move_pct(100, 105) == pytest.approx(5.0)
    assert alerts.daily_move_pct(100, 94) == pytest.approx(-6.0)
    assert alerts.daily_move_pct(0, 94) is None


def test_price_hits_thresholds():
    rows = [{"date": f"2026-10-{i:02d}", "close": 100.0, "volume": 100.0} for i in range(1, 22)]
    rows.append({"date": "2026-10-22", "close": 104.0, "volume": 199.0})  # 4%, 1.99x -> nothing
    rows.append({"date": "2026-10-23", "close": 109.2, "volume": 300.0})  # +5%, ~2.9x -> both
    hits = list(alerts.price_hits(rows, 2.0, 5.0, "2026-10-22"))
    kinds = {(k, d) for k, d, *_ in hits}
    assert ("price_move", "2026-10-23") in kinds
    assert ("volume_spike", "2026-10-23") in kinds
    assert not any(d == "2026-10-22" for _, d in kinds)


def _px(closes, vols=None):
    return [{"date": f"2026-{9 + i // 28:02d}-{1 + i % 28:02d}", "close": c, "volume": (vols or [100.0] * len(closes))[i]}
            for i, c in enumerate(closes)]


def test_volume_baseline_ignores_an_earlier_spike():
    # a 10x results day inside the window lifts a mean baseline to 145 and hides the next 2.5x day
    vols = [100.0] * 19 + [1000.0] + [250.0]
    assert alerts.volume_ratio(vols) == 2.5


def test_volume_burst_alerts_once_unless_it_escalates():
    vols = [100.0] * 21 + [300.0, 250.0, 400.0]
    hits = [d for _, d, *_ in alerts.price_hits(_px([100.0] * 24, vols), 2.0, None, "2026-09-01")]
    assert hits == ["2026-09-22", "2026-09-24"]  # 3.0x fires, 2.5x the next day doesn't, 4.0x does


def test_move_unusual_for_the_stock_fires_below_fixed_threshold():
    def walk(scale):  # 30 closes whose daily moves cycle through +-0.1..0.4% x scale
        out = [100.0]
        for i in range(29):
            out.append(out[-1] * (1 + scale * (0.4, -0.2, 0.1, -0.4, 0.2, -0.1)[i % 6] / 100))
        return out
    quiet = walk(1)
    rows = _px(quiet + [quiet[-1] * 1.03])                                  # then +3%
    hits = list(alerts.price_hits(rows, None, 5.0, rows[-1]["date"]))
    assert len(hits) == 1 and hits[0][0] == "price_move" and "σ" in hits[0][2]
    # the same +3% is the market's move, not the stock's: no alert
    assert not list(alerts.price_hits(rows, None, 5.0, rows[-1]["date"], {rows[-1]["date"]: 2.9}))
    # and a volatile stock moving 3% is just a normal day
    wild = walk(8)
    rows = _px(wild + [wild[-1] * 1.03])
    assert not list(alerts.price_hits(rows, None, 5.0, rows[-1]["date"]))


def test_magnitude_cues_demote_non_events():
    a = lambda subject, more="": {"subject": subject, "more": more, "headline": ""}
    assert push.magnitude_for(a("Credit Rating", "Intimation of re-affirmation of rating by S&P"), "credit_rating", None) == 0.5
    assert push.magnitude_for(a("Credit Rating", "Rating downgraded by ICRA"), "credit_rating", None) == 1.0
    assert push.magnitude_for(a("Acquisition", "Incorporation of Wholly Owned Subsidiary"), "m_and_a", None) == 0.4
    assert push.magnitude_for(a("Acquisition", "Acquisition of 100% stake in XYZ Ltd"), "m_and_a", None) == 1.0
    assert push.magnitude_for(a("Change in Management", "approved the re-appointment of Mr A"), "mgmt_change", None) == 0.6
    assert push.magnitude_for(a("Change in Management", "resignation of CFO and re-appointment of Mr A"), "mgmt_change", None) == 0.85
    assert push.score_for("results", 1.5, 1.0) == 1.0  # a reaction boost never pushes a score past 1


def _ann(symbol, ann_type, filed_at, category="Company Update"):
    from finn import db
    i = db.x("INSERT INTO announcements(content_hash,symbol,category,subject,filed_at,ann_type) VALUES(?,?,?,?,?,?)",
             (f"{symbol}{ann_type}{filed_at}{category}", symbol, category, "s", filed_at, ann_type))
    return db.q("SELECT * FROM announcements WHERE id=?", (i,), one=True)


def test_novelty_same_event_window():
    # two order wins two days apart are two events; two filings minutes apart are one
    a = _ann("NOV", "order_win", "2026-10-19T10:13:00+05:30")
    b = _ann("NOV", "order_win", "2026-10-21T09:45:00+05:30")
    c = _ann("NOV", "order_win", "2026-10-21T09:55:00+05:30")
    assert [push.novelty_for(x, "order_win") for x in (a, b, c)] == [1.0, 1.0, 0.5]
    # results: the 'Result' filing is primary even when the board-meeting outcome came first
    r1 = _ann("NOV", "results", "2026-10-29T14:03:00+05:30", "Board Meeting")
    r2 = _ann("NOV", "results", "2026-10-29T14:09:00+05:30", "Result")
    assert [push.novelty_for(x, "results") for x in (r1, r2)] == [0.25, 1.0]


def test_market_reaction_uses_the_first_session_that_could_trade_on_the_filing():
    from finn import db
    closes = [100.0 * (1.004 if i % 2 else 0.996) for i in range(30)] + [108.0]
    for r in _px(closes):
        db.x("INSERT INTO prices(symbol,date,close,volume) VALUES('RX',?,?,?)", (r["date"], r["close"], r["volume"]))
    jump, before = _px(closes)[-1]["date"], _px(closes)[-2]["date"]
    assert push.market_reaction(_ann("RX", "order_win", f"{before}T18:00:00+05:30"), "order_win")      # after close -> next session
    assert not push.market_reaction(_ann("RX", "order_win", f"{before}T10:00:00+05:30"), "order_win")  # traded that day: flat
    assert push.market_reaction(_ann("RX", "order_win", f"{jump}T10:00:00+05:30"), "order_win")
    assert not push.market_reaction(_ann("RX", "routine_compliance", f"{jump}T10:00:00+05:30"), "routine_compliance")
    # on a results day the move belongs to the results filing
    _ann("RX", "results", f"{jump}T12:00:00+05:30", "Result")
    assert not push.market_reaction(_ann("RX", "order_win", f"{jump}T11:00:00+05:30"), "order_win")


def test_alert_dedupe_per_rule_ticker_date():
    assert alerts._insert("price_move", "TEST", "2026-10-23", "t", "b", "/#x") is True
    assert alerts._insert("price_move", "TEST", "2026-10-23", "t2", "b2", "/#x") is False
    assert alerts._insert("price_move", "TEST", "2026-10-24", "t", "b", "/#x") is True


# ---------- quote verification ----------

PAGE = ("As these benefits gradually normalize, we expect the working capital cycle to trend towards our "
        "long-term operating range of 45 to 50 days. During the first half of the year, we incurred capital expenditures.")


def test_quote_ok_exact_and_fuzzy():
    assert grounding.quote_ok("we expect the working capital cycle to trend towards our long-term operating range of 45 to 50 days", PAGE)
    # small OCR / whitespace / curly-quote differences still pass
    assert grounding.quote_ok("we  expect the working-capital cycle to trend toward our long-term operating range of 45 to 50 days", PAGE)


def test_quote_rejects_fabrication():
    assert not grounding.quote_ok("we expect revenue to double by FY28 with margins of 20%", PAGE)


def test_quote_numbers_must_match_exactly():
    # one digit off is ~98% similar text, and a different fact
    assert not grounding.quote_ok("we expect the working capital cycle to trend towards our long-term operating range of 45 to 60 days", PAGE)


def test_value_must_be_printed_in_its_quote():
    assert grounding.value_in_quote(1600.0, "R&D spend of INR1,600 plus crores")
    assert grounding.value_in_quote(-5.0, "margins fell by 5%")
    assert not grounding.value_in_quote(7.5, "our estimate of the growth is anywhere between 7% to 8%")  # model's midpoint
    assert grounding.value_in_quote(None, "healthy growth")


def test_verify_drops_bad_items_and_fixes_page():
    pages = ["cover page", PAGE, "other text"]
    items = [{"quote": "long-term operating range of 45 to 50 days", "page": 2},
             {"quote": "long-term operating range of 45 to 50 days", "page": 3},   # off by one -> corrected to 2
             {"quote": "margins will reach 30 percent next year", "page": 2}]     # fabricated -> dropped
    kept = grounding.verify(items, pages)
    assert len(kept) == 2 and all(k["page"] == 2 for k in kept)


# ---------- research matching ----------

def test_parse_period():
    assert pull.parse_period("Q2 FY27") == (27, 2)
    assert pull.parse_period("FY27") == (27, 4)
    assert pull.parse_period("H1 FY2027") == (27, 2)
    assert pull.parse_period("FY26-FY28") == (28, 4)
    assert pull.parse_period("medium term") is None
    assert pull.parse_period("FY2025-26") == (26, 4)
    assert pull.parse_period("2025-26") == (26, 4)
    assert pull.parse_period("Q1 FY2026-27") == (27, 1)


def _g(**kw):
    base = {"quarter_said": "Q1 FY26", "speaker": "CFO", "metric": "EBITDA margin", "target_text": "13-15%",
            "target_low": 13.0, "target_high": 15.0, "unit": "%", "horizon": "FY26", "quote": "q", "page": 1}
    base.update(kw)
    return base


def test_verdicts():
    act = lambda v, p="FY26": {"period": p, "metric": "EBITDA margin", "value": v, "unit": "%"}
    assert pull.verdict_for(_g(), act(14.1), (26, 4)) == "delivered"
    assert pull.verdict_for(_g(), act(12.0), (26, 4)) == "partially"
    assert pull.verdict_for(_g(), act(9.0), (26, 4)) == "missed"
    assert pull.verdict_for(_g(target_low=None, target_high=None), None, (26, 4)) == "unverifiable"
    assert pull.verdict_for(_g(horizon="FY28"), None, (27, 2)) == "pending"


def test_match_links_guidance_to_actual_for_its_horizon():
    guidance = [_g(), _g(metric="revenue growth", target_text="20%", target_low=20, target_high=20, horizon="FY27")]
    actuals = [{"period": "FY26", "metric": "EBITDA margin", "value": 14.4, "unit": "%"},
               {"period": "H1 FY27", "metric": "revenue growth", "value": 47.0, "unit": "%"}]
    rows = pull.match(guidance, actuals)
    assert rows[0]["verdict"] == "delivered" and rows[0]["actual"]["value"] == 14.4
    assert rows[1]["verdict"] == "pending" and rows[1]["progress"]["value"] == 47.0


def test_match_needs_the_same_period_length():
    # a Q4 figure is not the full-year figure: FY27 stays open, Q4 shows as progress
    rows = pull.match([_g(horizon="FY27")], [{"period": "Q4 FY27", "metric": "EBITDA margin", "value": 9.0, "unit": "%"}])
    assert rows[0]["actual"] is None and rows[0]["progress"]["value"] == 9.0 and rows[0]["verdict"] != "missed"
    rows = pull.match([_g(horizon="H1 FY27")], [{"period": "Q2 FY27", "metric": "EBITDA margin", "value": 9.0, "unit": "%"}])
    assert rows[0]["actual"] is None


def test_match_prefers_the_actual_in_the_targets_unit():
    g = _g(metric="revenue growth", target_text="13% to 14%", target_low=13, target_high=14, horizon="FY26")
    actuals = [{"period": "FY26", "metric": "revenue", "value": 87584.0, "unit": "crore"},
               {"period": "FY26", "metric": "revenue growth", "value": 13.5, "unit": "%"}]
    for order in (actuals, actuals[::-1]):
        row = pull.match([g], order)[0]
        assert row["actual"]["value"] == 13.5 and row["verdict"] == "delivered"


def test_more_is_better_for_savings_and_reductions():
    act = lambda m, v: {"period": "FY26", "metric": m, "value": v, "unit": "crore"}
    g = lambda m: _g(metric=m, target_low=500, target_high=500, unit="crore")
    assert pull.verdict_for(g("cost savings"), act("cost savings", 600), (26, 4)) == "delivered"
    assert pull.verdict_for(g("debt reduction"), act("debt reduction", 300), (26, 4)) == "missed"
    assert pull.verdict_for(g("net debt"), act("net debt", 300), (26, 4)) == "delivered"


def test_repeated_promise_counts_once_and_keeps_latest():
    said = [_g(quarter_said="Q3 FY26", horizon="FY27", target_low=28, target_high=28),
            _g(quarter_said="Q1 FY27", horizon="FY27", target_low=28, target_high=None, metric="EBITDA margins"),
            _g(quarter_said="Q4 FY26", horizon="FY27", target_low=21, target_high=23),   # different target: kept
            _g(quarter_said="Q4 FY26", horizon="FY28", target_low=28, target_high=28)]   # different horizon: kept
    kept = pull.dedupe_guidance(said)
    assert len(kept) == 3
    assert [g["quarter_said"] for g in kept if g["horizon"] == "FY27" and g["target_low"] == 28] == ["Q1 FY27"]


def test_scorecard_hit_rate_needs_a_sample():
    rows = lambda *v: [{"verdict": x} for x in v]
    assert pull.scorecard(rows("delivered", "delivered", "partially", "missed", "pending")) == {"checked": 4, "hit_rate": 62}
    assert pull.scorecard(rows("delivered", "delivered", "pending")) == {"checked": 2, "hit_rate": None}


def test_scrub_numbers_removes_unsupported_sentences():
    allowed = pull._nums('{"value": 14.4, "target": "20%"}')
    text = "Margins reached 14.4% as guided (G1). Revenue will hit 5,000 crore next year."
    assert pull.scrub_numbers(text, allowed) == "Margins reached 14.4% as guided (G1)."


@pytest.mark.parametrize("a,b,expected", [
    ("FMEG EBITDA margin", "EBITDA margin", False),
    ("EBITDA margins", "EBITDA margin", True),
    ("Wires & Cables EBIT margins", "Wires and Cables EBIT margins", True),
    ("revenue growth", "consolidated revenue growth", True),
    ("capex", "capital expenditure", True),
    ("working capital cycle", "working capital days", True),
    ("revenue growth", "EBITDA margin", False),
])
def test_metric_match(a, b, expected):
    assert pull.metric_match(a, b) is expected


def test_open_horizon_gets_progress_and_tracking():
    g = _g(horizon="medium term", target_low=11, target_high=13)
    rows = pull.match([g], [{"period": "Q2 FY27", "metric": "EBITDA margin", "value": 14.4, "unit": "%"},
                            {"period": "Q1 FY27", "metric": "EBITDA margin", "value": 13.2, "unit": "%"}])
    assert rows[0]["verdict"] == "pending"
    assert rows[0]["progress"]["value"] == 14.4 and rows[0]["tracking"] == "on track"
