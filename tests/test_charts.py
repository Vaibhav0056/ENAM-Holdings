"""Chart specs (finn/charts.py) are computed in Python from verified data; static/charts.js only draws them."""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finn import charts

ROOT = Path(__file__).resolve().parent.parent


def _results_card(sqldb_session, cid, fid, period, rows, lines=None, filed=None):
    from finn.models import Enrichment, Filing
    s = sqldb_session
    s.add(Filing(id=fid, company_id=cid, exchange="BSE", exchange_id=str(fid), subject="Results", content_hash=str(fid) * 8,
                 filed_at=filed or datetime(2026, 10, 15, tzinfo=timezone.utc)))
    s.add(Enrichment(filing_id=fid, type="results", bucket="attention", score=1.0,
                     payload={"ok": True, "period": period, "basis": "consolidated", "document_id": 9, "notes": [],
                              "table": rows, "lines": lines or {}}))


def row(metric, cur, prev, ya, page=4):
    return {"metric": metric, "found": True, "current": cur, "previous": prev, "year_ago": ya, "page": page, "quote": f"{metric} line"}


@pytest.fixture()
def co(sqldb):
    from finn.database import session
    from finn.models import Company
    with session() as s:
        s.add(Company(id=1, nse_symbol="AAA", name="AAA Ltd", is_portfolio=True))
        _results_card(s, 1, 10, "Q2 FY27", [row("revenue", 1200, 1100, 1000), row("ebitda", 240, 200, 180), row("pat", 150, 140, 120)],
                      lines={"revenue": 1200, "ebitda": 240, "depreciation": 30, "finance_costs": 10, "other_income": 20,
                             "pbt": 220, "pat": 150})
    return 1


def test_results_chart_keeps_gaps_and_states_the_finding(co):
    spec = charts.results_chart(1, "AAA")
    assert spec["x"] == ["Q3 FY25", "Q4 FY25", "Q1 FY26", "Q2 FY26", "Q3 FY26", "Q4 FY26", "Q1 FY27", "Q2 FY27"]
    rev = spec["series"][0]["values"]
    assert rev[-1] == 1200 and rev[-2] == 1100 and rev[3] == 1000 and rev[0] is None      # missing quarters stay None
    assert spec["series"][2]["values"][-1] == 20.0                                       # EBITDA margin 240 / 1200
    assert "revenue up 20.0% YoY in Q2 FY27" in spec["title"] and "from 18.0% a year ago" in spec["title"]
    assert spec["sources"][0][-1]["page"] == 4 and spec["sources"][0][-1]["document_id"] == 9
    assert "3 of 8 quarters" in spec["note"] and spec["table"]["rows"][0][1] == "Not in sources"


def test_margin_bridge_steps_add_up_and_name_a_real_step(co):
    spec = charts.margin_bridge(1, "AAA")
    vals, steps = spec["series"][0]["values"], spec["series"][0]["steps"]
    assert vals[0] == 20.0 and vals[-1] == 12.5                                          # 240 / 1200, 150 / 1200
    assert abs(vals[0] + sum(vals[1:4]) - vals[4]) < 0.02                                # EBITDA − D − F + OI = PBT
    assert abs(vals[4] + vals[5] - vals[6]) < 0.02                                       # PBT − tax = PAT
    assert "biggest step is tax and other items" in spec["title"]                        # a step, never a subtotal


def test_hit_rate_and_drift_from_a_report():
    rep = {"symbol": "AAA", "ledger": [{"said_in": "Q1 FY27", "verdict": "delivered"}, {"said_in": "Q1 FY27", "verdict": "pending"},
                                       {"said_in": "Q2 FY27", "verdict": "missed"}, {"said_in": "Q2 FY27", "verdict": "pending"}],
           "drift": [{"metric": "capex", "horizon": "FY27", "mid_unit": "₹ crore",
                      "steps": [{"from": "Q1 FY27", "to": "Q2 FY27", "change": "raised", "before_mid": 1400.0, "after_mid": 1600.0}]}]}
    h = charts.hit_rate_chart(rep)
    assert h["x"] == ["Q1 FY27", "Q2 FY27"] and h["title"].startswith("1 of 2 matured promises delivered; 2 not yet due")
    d = charts.drift_chart(rep)
    assert d["kind"] == "step" and d["series"][0]["values"] == [1400.0, 1600.0] and "raised" in d["title"]


def test_deep_charts_from_deep_dive_data():
    cash = charts.deep_charts({"recipe": "cash_quality", "report": "Annual Report 2026", "document_id": 3,
                               "inputs": {"current": {"ocf": 3810.0, "pat": 2800.0}, "previous": {"ocf": 2000.0, "pat": 2500.0}},
                               "evidence": {"current": {"ocf": {"page": 319, "quote": "Net cash from operating activities"}}}})
    assert cash[0]["series"][2]["values"] == [0.8, 1.36] and "1.36× PAT" in cash[0]["title"]
    assert cash[0]["sources"][0][1]["page"] == 319
    worry = charts.deep_charts({"recipe": "worry_map", "topics": ["demand", "pricing"], "quarters": ["Q1 FY27", "Q2 FY27"],
                                "counts": {"demand": {"Q1 FY27": 3, "Q2 FY27": 6}}, "rising": ["demand"], "questions": 9})
    assert worry[0]["grid"]["values"] == [[3, 6], [0, 0]] and "Rising on the latest call: demand" in worry[0]["title"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_every_chart_kind_renders_without_gaps_drawn_as_numbers():
    specs = [charts.hit_rate_chart({"symbol": "A", "ledger": [{"said_in": "Q1 FY27", "verdict": "delivered"}]}),
             {"kind": "bars", "x": ["Q1 FY27", "Q2 FY27"], "series": [{"name": "Revenue", "values": [None, 10.0], "as": "bar", "unit": "₹ cr"},
                                                                    {"name": "m", "values": [None, 5.0], "as": "line", "unit": "%"}]},
             {"kind": "waterfall", "x": ["a", "b", "c"], "axis": ["a", "b", "c"],
              "series": [{"name": "%", "values": [10.0, -2.0, 8.0], "steps": ["total", "delta", "total"], "unit": "%"}]},
             {"kind": "band", "x": ["Q1", "Q2"], "series": [{"name": "k", "values": [12.0, None], "as": "line", "unit": "%"}],
              "threshold": {"value": 10, "label": "threshold 10%", "op": ">="}},
             {"kind": "heat", "x": ["Q1"], "series": [], "grid": {"rows": ["demand"], "cols": ["Q1"], "values": [[2]]}},
             {"kind": "step", "x": ["Q1", "Q2"], "series": [{"name": "c", "values": [1.0, 2.0], "as": "line", "unit": ""}]},
             {"kind": "spark", "x": ["Q1", "Q2", "Q3"], "series": [{"name": "h", "values": [1.0, 2.0, 1.5], "as": "line", "unit": ""}]},
             {"kind": "price", "x": [f"2026-01-{d:02d}" for d in range(1, 31)],
              "series": [{"name": "Close", "values": [100.0 + d for d in range(30)], "as": "line", "unit": "₹"}],
              "events": [{"i": 3, "kind": "results", "label": "Results"}]}]
    script = ("global.window={}; global.performance={now:()=>Date.now()}; require(process.argv[1]);"
              "const specs=JSON.parse(require('fs').readFileSync(0,'utf8')); specs.forEach(s=>window.FinnCharts.render(s));"
              "const out=specs.map(s=>{const t=process.hrtime.bigint(); const svg=window.FinnCharts.render(s);"
              "return {kind:s.kind, ok:svg.startsWith('<svg') && !/NaN|undefined/.test(svg), ms:Number(process.hrtime.bigint()-t)/1e6}});"
              "console.log(JSON.stringify(out))")
    res = subprocess.run(["node", "-e", script, str(ROOT / "static" / "charts.js")], input=json.dumps(specs),
                         capture_output=True, text=True, timeout=60)
    out = json.loads(res.stdout)
    assert all(r["ok"] for r in out), out
    assert max(r["ms"] for r in out) < 50



def test_guidance_band_uses_only_a_company_wide_ebitda_target(co):
    from finn.database import session
    from finn.models import Report
    ledger = [{"metric": "Wires & Cables EBITDA margin", "unit": "%", "target_low": 11, "target_high": 13, "said_in": "Q2 FY27"},
              {"metric": "operating margin", "unit": "%", "target_low": 26, "target_high": 28, "said_in": "Q2 FY27"},
              {"metric": "consolidated EBITDA margin", "unit": "%", "target_low": 18, "target_high": 22, "said_in": "Q1 FY27",
               "quote": "we expect 18% to 22% EBITDA", "page": 7, "doc": "D1"}]
    with session() as s:
        s.add(Report(company_id=1, kind="core", data={"ledger": ledger, "sources": [{"id": "D1", "title": "Earnings call", "document_id": 5}]},
                     receipt={}))
    spec = charts.guidance_band(1, "AAA")
    assert spec["threshold"]["value"] == 18.0 and "18–22% target (said Q1 FY27)" in spec["title"]
    assert "Not your thesis" in spec["note"] and spec["sources"][0][0]["page"] == 7


def test_unavailable_deep_dive_is_a_designed_state_not_an_error(monkeypatch):
    from fastapi.testclient import TestClient
    import api
    from finn.deep import engine as deep

    def needs(symbol, recipe, force=False):
        raise deep.NeedsResearch("Peer compare reads both companies' research reports; HAVELLS has none yet", research=["HAVELLS"])
    monkeypatch.setattr(deep, "run", needs)
    r = TestClient(api.app).get("/api/v2/deep/POLYCAB/peer_compare")
    assert r.status_code == 200
    assert r.json() == {"recipe": "peer_compare", "unavailable": "Peer compare reads both companies' research reports; "
                        "HAVELLS has none yet", "research": ["HAVELLS"], "charts": []}



def test_reports_never_show_raw_errors():
    from finn.research.engine import present
    rep = {"notes": ["Counter-argument unavailable: devils_advocate: all strong-tier models failed: 504 DEADLINE_EXCEEDED. {'error': 1}"],
           "sources": [{"kind": "transcript", "status": "failed", "reason": "403 Client Error: Forbidden for url: https://x"},
                       {"kind": "transcript", "status": "read"}, {"kind": "annual_report", "status": "read"}]}
    out = present(rep)
    text = " ".join(out["notes"]) + " " + " ".join(s.get("reason") or "" for s in out["sources"])
    assert "504" not in text and "403" not in text and "{" not in text and "http" not in text
    assert out["notes"][0].startswith("1 of 2 earnings-call transcripts could not be read (the publisher's website refused")
    assert "the model service took too long to answer" in out["notes"][1]
