"""Phase 2: rules, scoring factors, novelty, headlines, windows and the digest query."""
from datetime import datetime, timedelta, timezone

import pytest

from finn.feed import classify as cls
from finn.feed import digest as dg
from finn.feed import score as sc
from finn.feed.pipeline import first_sentence, headline

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("cat,sub,subj,expected", [
    ("Result", "Financial Results", "Financial Results for the quarter ended September 30, 2026", "results"),
    ("Company Update", "General", "Reference To The Financial Results Issued By Suzuki Motor Corporation", "other"),
    ("Company Update", "General", "Resignation of Statutory Auditor", "governance"),
    ("Company Update", "Statutory Auditor", "Appointment of statutory auditor", "routine_compliance"),
    ("Company Update", "General", "Receipt Of Approval Of The Shareholders For Reclassification Of Shareholder Category", "routine_compliance"),
    ("AGM/EGM", "EGM", "Shareholder Meeting / Postal Ballot-Outcome of EGM", "agm_egm"),
    ("Board Meeting", "Board Meeting", "Board Meeting Intimation for results", "board_meeting_notice"),
    ("Company Update", "Qualified Institutional Placement", "Announcement under Regulation 30 (LODR)-Qualified Institutional Placement", "fundraise"),
    ("Company Update", "General", "Intimation Of ESG Rating", "routine_compliance"),
    ("Company Update", "Credit Rating", "Credit Rating", "credit_rating"),
    ("Company Update", "General", "Q2 & H1 FY 2026-2027 Results Presentation", "investor_meet"),
    ("Company Update", "Press Release / Media Release", "Press Release / Media Release", None),
])
def test_rules(cat, sub, subj, expected):
    assert cls.rule_classify(cat, sub, subj) == expected


def test_classify_without_llm_falls_back_to_other():
    out = cls.classify([{"id": 1, "category_raw": "Company Update", "subcategory_raw": "General", "subject": "Store update"}],
                       use_llm=False)
    assert out == {1: ("other", "fallback")}


def _compute(ftype, subject="", body="", siblings=(), **kw):
    return sc.compute(ftype, filed_at=kw.pop("filed_at", T0), filing_id=kw.pop("filing_id", 1), category=kw.pop("category", None),
                      subject=subject, body=body, siblings=list(siblings), **kw)


def test_score_breakdown_and_buckets():
    b = _compute("results", results_extracted=True, pat_yoy=30.0)
    assert (b.score, b.bucket) == (1.0, "attention") and "PAT +30.0% YoY" in b.reasons
    assert _compute("routine_compliance").bucket == "routine"
    assert _compute("other").bucket == "worth_a_look"
    d = _compute("credit_rating", body="Ratings reaffirmed by CRISIL").as_dict()
    assert d["magnitude"] == 0.5 and d["bucket"] == "worth_a_look" and "formula" in d


def test_senior_management_change_is_full_weight():
    assert _compute("mgmt_change", body="Change in Managing Director and Chief Executive Officer").score == 1.0
    assert _compute("mgmt_change", body="Appointment of Head of HR").score == round(0.9 * 0.85, 3)
    assert _compute("mgmt_change", body="re-appointment of Mr X as Independent Director").bucket == "worth_a_look"


def test_order_size_cues_and_unknown_value():
    assert _compute("order_win", body="L&T Wins Orders (Mega*) for Power T&D").score == round(0.7 * 1.2, 3)
    assert _compute("order_win", body="L&T Wins Orders (Large*) in New Delhi").bucket == "worth_a_look"
    assert _compute("order_win", body="TCS to harmonise Metro's IT landscape").bucket == "worth_a_look"
    assert _compute("order_win", body="TCS wins Rs 122 crore bid").score == 0.7


def test_results_twin_is_folded_under_the_primary():
    twin = {"id": 2, "filed_at": T0 + timedelta(minutes=5), "category_raw": "Result"}
    b = _compute("results", category="Board Meeting", siblings=[twin], results_extracted=True, pat_yoy=10)
    assert b.novelty == 0.25 and b.bucket == "routine"


def test_strongest_filing_leads_the_event_and_updates_need_an_earlier_filing():
    update = {"id": 2, "filed_at": T0 - timedelta(hours=1), "category_raw": None,
              "subject": "Update On Voluntary Public Takeover Offer", "body": "An update on the offer"}
    big = _compute("m_and_a", subject="Press Release", body="Persistent Secures Strong Majority Stake", siblings=[update])
    assert big.novelty == 1.0 and big.score == 1.0
    first_update = _compute("m_and_a", subject="Update On The Public Takeover Offer", body="Update on the offer")
    assert first_update.novelty == 1.0 and first_update.bucket == "attention"
    older = {"id": 3, "filed_at": T0 - timedelta(days=10), "category_raw": None, "subject": "x", "body": "y"}
    later_update = _compute("m_and_a", subject="Update On The Public Takeover Offer", body="Update", siblings=[older])
    assert later_update.novelty == 0.5


def test_market_reaction_boost_only_for_primary_substantive_filings():
    moved = {"hit": True, "move_pct": -6.2, "z": 4.1}
    assert "stock moved -6.2%" in " ".join(_compute("order_win", body="Rs 900 crore order", reacted=moved).reasons)
    assert _compute("investor_meet", reacted=moved).magnitude == 1.0
    assert _compute("order_win", body="Rs 900 crore order", reacted=moved, results_same_day=True).magnitude == 1.0


def test_feedback_adjustment_is_bounded():
    w, adj = sc.type_weight("other", feedback={"other": 0.5})
    assert (w, adj) == (0.45, 0.15)


@pytest.mark.parametrize("body,expected", [
    ("Retirement of Mr. Noel. N. Tata as Director w.e.f. 5 Nov 2026. Details enclosed.",
     "Retirement of Mr. Noel. N. Tata as Director w.e.f. 5 Nov 2026."),
    ("This is to inform you that the Board approved a dividend. More.", "the Board approved a dividend."),
    ("As per annexure enclosed", ""),
])
def test_first_sentence(body, expected):
    assert first_sentence(body) == expected


def test_headline_prefers_summary_over_generic_subjects():
    assert headline("Announcement under Regulation 30 (LODR)-Credit Rating", "Credit Rating by ICRA Limited for NCDs",
                    "Credit Rating") == "Credit Rating by ICRA Limited for NCDs"
    assert headline("Change in Management", "As per annexure enclosed", "Change in Management") == "Change in Management"
    assert headline("Acquisition of 51% stake in XYZ", "", "Acquisition") == "Acquisition of 51% stake in XYZ"


def test_windows():
    now = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)
    assert dg.resolve_window("7d", now=now)["from"] == now - timedelta(days=7)
    w = dg.resolve_window(None, "2026-10-27", "2026-10-31", now=now)
    assert w["from"] == datetime(2026, 10, 26, 18, 30, tzinfo=timezone.utc) and w["to"] == datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)
    last = now - timedelta(hours=30)
    assert dg.resolve_window("since_last", now=now, last_checked=last)["from"] == last
    assert dg.resolve_window("since_last", now=now)["label"].startswith("Last 24 hours")
    for bad in [("1y", None, None), (None, "2026-01-01", "2026-10-31"), (None, "2026-10-31", "2026-10-01"), (None, "2026-10-01", None)]:
        with pytest.raises(dg.WindowError):
            dg.resolve_window(*bad, now=now)


def test_digest_groups_buckets_and_excludes_peers(sqldb):
    from finn.database import session
    from finn.models import Company, Enrichment, Filing
    now = datetime.now(timezone.utc)
    with session() as s:
        s.add_all([Company(id=1, nse_symbol="TCS", name="TCS", is_portfolio=True),
                   Company(id=2, nse_symbol="KPITTECH", name="KPIT", is_portfolio=False)])
        for i, (cid, bucket, score) in enumerate([(1, "attention", 1.0), (1, "worth_a_look", 0.4), (1, "routine", 0.05),
                                                  (2, "attention", 0.9)], start=1):
            s.add(Filing(id=i, company_id=cid, exchange_id=str(i), filed_at=now - timedelta(hours=i), subject=f"s{i}",
                         content_hash=f"h{i}"))
            s.add(Enrichment(filing_id=i, type="other", score=score, bucket=bucket, headline=f"h{i}", score_breakdown={}))
    dg.invalidate()
    out = dg.digest("24h")
    assert out["counts"] == {"attention": 1, "worth_a_look": 1, "routine": 1}
    assert out["attention"][0]["symbol"] == "TCS" and not out["caught_up"]
    assert dg.mark_reviewed(1) and dg.mark_reviewed(2)
    assert dg.digest("24h")["caught_up"] is True
    assert dg.digest("24h", include_peers=True)["counts"]["attention"] == 2
