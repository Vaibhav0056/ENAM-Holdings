"""Phase 9: answer verification (citations, numbers, advice), the fallback plan, snapshot lookup."""
from finn import copilot as C
from finn import snapshot

EV = [{"text": "[POLYCAB Q2 FY27 results] revenue ₹9,899 crore (YoY 52.8%)", "source": {"kind": "results_card"}},
      {"text": "No passages found.", "source": {"kind": "note"}}]


def test_verify_answer_keeps_cited_supported_sentences_only():
    a = C.Answer(not_in_sources=False, sentences=[
        C.Sentence(text="Revenue grew 52.8% year on year.", citations=["E1"]),
        C.Sentence(text="Revenue grew 61% year on year.", citations=["E1"]),        # number not in evidence
        C.Sentence(text="Polycab is a strong company.", citations=[]),              # uncited
        C.Sentence(text="Investors should buy on dips.", citations=["E1"]),         # advice
        C.Sentence(text="Revenue was ₹9,899 crore [E1].", citations=["E1", "E9"])])  # unknown id dropped, tag stripped
    out, removed = C.verify_answer(a, EV)
    assert [s["text"] for s in out] == ["Revenue grew 52.8% year on year.", "Revenue was ₹9,899 crore."]
    assert out[1]["citations"] == ["E1"] and removed == 3


def test_not_in_sources_answer_may_stand_without_citations():
    a = C.Answer(not_in_sources=True, sentences=[C.Sentence(text="FINN has no data on market share in China.", citations=[])])
    out, _ = C.verify_answer(a, EV)
    assert out == [{"text": "FINN has no data on market share in China.", "citations": []}]


def test_fallback_plan_runs_when_steps_gather_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(C.router, "call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 UNAVAILABLE")))
    for name in ("search_docs", "get_filings", "get_ledger"):
        monkeypatch.setitem(C.TOOLS, name, lambda _n=name, **kw: calls.append(_n) or [{"text": f"{_n} ok", "source": {"kind": "note"}}])
    monkeypatch.setattr(C, "spent", lambda b: 0.0)
    r = C.ask("What is the working capital guidance?", "POLYCAB")
    assert calls == ["search_docs", "get_filings", "get_ledger"] and r["not_in_sources"] is True


def test_snapshot_lookup_routes(monkeypatch):
    monkeypatch.setattr(snapshot, "_data", {"generated_at": "t", "digest": {"7d": {"counts": 1}}, "holdings": {"holdings": []},
                                            "holding_pages": {"TCS": {"symbol": "TCS"}}, "alerts": {"alerts": []},
                                            "catalysts": {"events": []}, "research": {"POLYCAB": {"name": "Polycab"}}})
    assert snapshot.lookup("/digest", {"window": "7d"}) == {"counts": 1}
    assert snapshot.lookup("/api/v2/holdings/tcs", {}) == {"symbol": "TCS"}
    assert snapshot.lookup("/api/v2/research/POLYCAB", {}) == {"name": "Polycab"}
    assert snapshot.lookup("/api/v2/research/job/abc", {}) is None
