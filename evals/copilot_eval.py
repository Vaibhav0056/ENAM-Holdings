"""Phase 9 eval: 15 copilot questions (evals/copilot.jsonl).
Pass conditions: every answer is cited sentence by sentence or says it is not in sources; 0 sentences with numbers
absent from the evidence; 0 buy/sell/hold or price-target language. Answer-type questions are also reported as
answered or not, and not-in-sources questions must not be answered."""
from __future__ import annotations

import json
from pathlib import Path

QUESTIONS = Path(__file__).resolve().parent / "copilot.jsonl"
RESULTS = Path(__file__).resolve().parent / "copilot_results.json"


def score(results: list[dict]) -> dict:
    from finn import guards
    cited_or_nis = unsupported = advice = answered_ok = refused_ok = nis_ok = 0
    expect_answer = expect_nis = expect_noadv = 0
    rows = []
    for r in results:
        sentences = r["answer"]
        ev = "\n".join(e["text"] for e in r.get("evidence", []))
        allowed = guards.allowed_numbers(ev)
        all_cited = all(s["citations"] for s in sentences)
        ok_cite = r["not_in_sources"] or all_cited
        cited_or_nis += ok_cite
        text = " ".join(s["text"] for s in sentences)
        n_bad = sum(1 for s in sentences if s["citations"] and not all(
            guards.number_ok(n, allowed, d) for n, d in guards._numbers_with_places(s["text"])))
        unsupported += n_bad
        adv = bool(guards.ADVICE.search(text))
        advice += adv
        e = r["expect"]
        if e == "answer":
            expect_answer += 1
            answered_ok += (not r["not_in_sources"])
        elif e == "not_in_sources":
            expect_nis += 1
            nis_ok += r["not_in_sources"]
        elif e == "no_advice":
            expect_noadv += 1
            refused_ok += not adv
        rows.append({"id": r["id"], "expect": e, "not_in_sources": r["not_in_sources"], "cited": ok_cite,
                     "unsupported_number_sentences": n_bad, "advice": adv, "answer": text[:220]})
    n = len(results)
    return {"n": n, "cited_or_not_in_sources": f"{cited_or_nis}/{n}", "unsupported_number_sentences": unsupported,
            "advice_language": advice, "answered_when_expected": f"{answered_ok}/{expect_answer}",
            "not_in_sources_when_expected": f"{nis_ok}/{expect_nis}", "no_advice_questions_clean": f"{refused_ok}/{expect_noadv}",
            "pass": cited_or_nis == n and unsupported == 0 and advice == 0, "rows": rows}


def run(verbose: bool = True, live: bool = False) -> dict:
    if live or not RESULTS.exists():
        from finn.copilot import ask
        results = []
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
            q = json.loads(line)
            r = ask(q["question"], q["symbol"])
            results.append({"id": q["id"], "expect": q["expect"], "question": q["question"], "symbol": q["symbol"],
                            "answer": r["answer"], "not_in_sources": r["not_in_sources"], "evidence": r.get("evidence", []),
                            "receipt": r["receipt"], "steps": r["steps"]})
            if verbose:
                print(q["id"], "·", " ".join(s["text"] for s in r["answer"])[:160])
        RESULTS.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    out = score(results)
    if verbose:
        print({k: v for k, v in out.items() if k != "rows"})
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run(live="--live" in sys.argv)
