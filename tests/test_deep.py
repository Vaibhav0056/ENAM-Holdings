"""Phase 8 maths: hedge rate, cash quality, balance sheet, language diff, worry grid, ledger comparison, chunking."""
import pytest

from finn.deep import maths as M
from finn.retrieval import _split


def test_hedge_rate_counts_phrases_per_thousand_words():
    r = M.hedge_rate(["We expect demand to stay soft. It may improve.", "Margins could see pressure."])
    assert r["words"] == 13 and r["hedges"] == 5 and r["per_1000_words"] == pytest.approx(384.62, abs=0.01)
    assert M.hedge_rate([])["per_1000_words"] is None


def test_cash_quality_ratios_days_and_checks():
    m = M.cash_quality({"ocf": 120, "pat": 100, "capex": -40, "receivables": 50, "inventories": 60, "payables": 30,
                        "revenue": 365, "cogs": 219})
    assert m["ocf_to_pat"] == 1.2 and m["fcf"] == 80 and m["fcf_to_pat"] == 0.8
    assert (m["receivable_days"], m["inventory_days"], m["payable_days"]) == (50.0, 100.0, 50.0)
    assert m["cash_conversion_cycle"] == 100.0 and m["checks"] == []
    odd = M.cash_quality({"receivables": 1, "revenue": 3650})
    assert odd["receivable_days"] == 0.1 and "unusual" in odd["checks"][0]


def test_balance_sheet_net_cash_is_negative_net_debt():
    b = M.balance_sheet({"borrowings_short": 93, "cash": 390, "current_investments": 3405, "ebitda": 4242,
                         "finance_costs": 243, "equity": 10000})
    assert b["net_debt"] == -3702 and b["net_debt_to_ebitda"] == pytest.approx(-0.87, abs=0.01)
    assert b["interest_cover"] == 17.5 and b["debt_to_equity"] == 0.01
    assert M.balance_sheet({})["net_debt"] is None


def test_language_diff_flags_unmatched_and_puts_numbers_first():
    a = {"text": "Capex will be 1,000 crore", "embedding": [1, 0, 0]}
    b = {"text": "Demand is healthy", "embedding": [0, 1, 0]}
    c = {"text": "Demand stays healthy", "embedding": [0, 0.99, 0.1]}
    d = M.language_diff([a, b], [c])
    assert [x["text"] for x in d["dropped"]] == ["Capex will be 1,000 crore"] and d["new"] == []


def test_worry_grid_spots_a_rising_topic():
    labels = [{"quarter": "Q4 FY26", "topic": "demand"}, {"quarter": "Q1 FY27", "topic": "demand"},
              {"quarter": "Q2 FY27", "topic": "competition"}, {"quarter": "Q2 FY27", "topic": "competition"},
              {"quarter": "Q2 FY27", "topic": "competition"}]
    g = M.worry_grid(labels)
    assert g["quarters"] == ["Q4 FY26", "Q1 FY27", "Q2 FY27"] and g["rising"] == ["competition"]


def test_compare_ledgers():
    def rep(sym, score):
        return {"symbol": sym, "name": sym, "credibility": {"score": score, "measured": 3, "total": 10, "vagueness_pct": 20,
                                                            "counts": {"delivered": 2, "missed": 1}}, "drift": [{"dropped": True}]}
    out = M.compare_ledgers(rep("A", 80), rep("B", None))
    assert [r["score"] for r in out["rows"]] == [80, None] and out["rows"][0]["dropped_guidance"] == 1


def test_chunk_split_respects_the_limit():
    parts = _split("Sentence one. " * 300, 200)
    assert all(len(p) <= 200 for p in parts) and len(parts) > 10
