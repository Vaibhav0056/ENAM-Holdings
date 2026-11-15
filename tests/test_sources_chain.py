"""Source adapters (recorded real responses in tests/fixtures/sources), failover between sources, the circuit
breaker, validation and cross-exchange dedupe. No network."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

FX = Path(__file__).parent / "fixtures" / "sources"


def co(i, sym, bse=None):
    return SimpleNamespace(id=i, nse_symbol=sym, bse_code=bse, yahoo_symbol=f"{sym}.NS", status="ok")


# ---------- parsers on recorded responses ----------

def test_bse_rows_map_to_filings():
    from finn.sources.bse_filings import to_filing
    raw = json.loads((FX / "bse_announcements_500180.json").read_text(encoding="utf-8"))["Table"]
    rows = [to_filing(r, 7) for r in raw]
    assert rows and all(r["exchange"] == "BSE" and r["company_id"] == 7 and len(r["content_hash"]) == 64 for r in rows)
    assert all(r["filed_at"].tzinfo is not None for r in rows)


def test_nse_api_parser():
    from finn.sources import nse
    rows = nse.parse_api(json.loads((FX / "nse_announcements_TCS.json").read_text(encoding="utf-8")), 3)
    r = rows[0]
    assert r["exchange"] == "NSE" and r["company_id"] == 3 and r["pdf_url"].startswith("https://")
    # 09-Nov-2026 17:12:15 IST is 11:42:15 UTC
    assert r["filed_at"] == datetime(2026, 11, 9, 11, 42, 15, tzinfo=timezone.utc)
    assert r["subject"].startswith("Analysts") and "Schedule of meet" in r["body"]


def test_nse_rss_parser_takes_symbol_from_pdf_name():
    from finn.sources import nse
    xml = (FX / "nse_rss_announcements.xml").read_text(encoding="utf-8")
    first_link = xml.split("<item>", 1)[1].split("<link>", 1)[1].split("</link>", 1)[0]
    sym = first_link.rsplit("/", 1)[-1].split("_", 1)[0]
    rows = nse.parse_rss(xml, {sym: 42})
    assert rows and all(r["company_id"] == 42 and r["exchange"] == "NSE" for r in rows)
    assert nse.parse_rss(xml, {"NOT_A_SYMBOL": 1}) == []


def test_nse_event_calendar_parser():
    from finn.sources import nse
    ev = json.loads((FX / "nse_event_calendar.json").read_text(encoding="utf-8"))
    rows = nse.parse_events(ev, {ev[0]["symbol"]: 9})
    assert rows[0]["company_id"] == 9 and rows[0]["kind"] == "results" and rows[0]["event_date"] == date(2026, 11, 16)


def test_bhavcopy_parser():
    from finn.sources import nse
    rows = nse.parse_bhavcopy((FX / "nse_bhavcopy_20261113.csv").read_text(encoding="utf-8"),
                              {"HDFCBANK": (5, "HDFCBANK.NS")})
    assert len(rows) == 1
    r = rows[0]
    assert r == {"company_id": 5, "symbol": "HDFCBANK.NS", "date": date(2026, 11, 13), "open": 808.0, "high": 808.8,
                 "low": 798.0, "close": 804.0, "adj_close": 804.0, "volume": 23442886.0}


# ---------- validation ----------

def test_validation_rejects_bad_rows():
    from finn.sources import chain
    at = datetime(2026, 11, 15, tzinfo=timezone.utc)
    good = {"company_id": 1, "subject": "Outcome", "filed_at": at - timedelta(hours=2), "pdf_url": "https://x/y.pdf",
            "content_hash": "a" * 64}
    assert chain.valid_filing(good, {1}, at)
    assert not chain.valid_filing({**good, "company_id": 2}, {1}, at)                  # unknown company
    assert not chain.valid_filing({**good, "filed_at": at + timedelta(days=3)}, {1}, at)  # from the future
    assert not chain.valid_filing({**good, "subject": " "}, {1}, at)
    assert not chain.valid_filing({**good, "pdf_url": "http://x"}, {1}, at)
    p = {"close": 10.0, "high": 11.0, "low": 9.0, "date": date(2026, 11, 13)}
    assert chain.valid_price(p)
    assert not chain.valid_price({**p, "close": 0}) and not chain.valid_price({**p, "close": float("nan")})
    assert not chain.valid_price({**p, "high": 8.0})


# ---------- chain behaviour (SQLite) ----------

@pytest.fixture()
def cos(sqldb):
    from finn.database import session
    from finn.models import Company
    with session() as s:
        for i, sym in enumerate(["AAA", "BBB", "CCC"], 1):
            s.add(Company(id=i, nse_symbol=sym, name=sym, bse_code=str(500000 + i), yahoo_symbol=f"{sym}.NS", is_portfolio=True))
    return [co(1, "AAA", "500001"), co(2, "BBB", "500002"), co(3, "CCC", "500003")]


def _row(cid, exch, minutes, subject="Outcome of board meeting", pdf=None, hid=None):
    return {"company_id": cid, "exchange": exch, "exchange_id": hid or f"{exch}{cid}{minutes}",
            "filed_at": datetime.now(timezone.utc) - timedelta(minutes=minutes), "category_raw": None,
            "subcategory_raw": None, "subject": subject, "body": "The board approved the results.", "pdf_url": pdf,
            "xbrl_url": None, "content_hash": (f"{exch}{cid}{minutes}{subject}" * 8)[:64].ljust(64, "x"), "status": "new", "raw": {}}


def test_failover_hands_failed_companies_to_the_next_source(cos):
    from finn.sources import chain
    calls = []

    def bse(cs):
        calls.append(("bse", [c.nse_symbol for c in cs]))
        raise RuntimeError("HTTP 403 Forbidden")

    def nse_api(cs):
        calls.append(("nse_api", [c.nse_symbol for c in cs]))
        return [_row(1, "NSE", 30), _row(2, "NSE", 40)], [cs[2]]          # CCC failed here

    def rss(cs):
        calls.append(("rss", [c.nse_symbol for c in cs]))
        return [_row(3, "NSE", 50)], []

    rep = chain.run_chain("announcements", [("bse_announcements", bse), ("nse_api_announcements", nse_api),
                                            ("nse_rss_announcements", rss)], cos, chain.write_filings,
                          lambda r: chain.valid_filing(r, {1, 2, 3}, chain.now()))
    assert calls == [("bse", ["AAA", "BBB", "CCC"]), ("nse_api", ["AAA", "BBB", "CCC"]), ("rss", ["CCC"])]
    assert rep["nse_api_announcements"]["added"] == 2 and rep["nse_rss_announcements"]["added"] == 1 and rep["unserved"] == []
    assert "403" in rep["bse_announcements"]["error"]


def test_circuit_breaker_opens_after_three_failures_and_half_opens(cos):
    from finn.sources import chain
    t0 = chain.now()
    for i in range(chain.BREAKER_FAILS):
        assert chain.breaker_open("bse_announcements", t0) is None
        chain.record("bse_announcements", False, error="403", at=t0)
    until = chain.breaker_open("bse_announcements", t0)
    assert until and abs(until - (t0 + chain.BREAKER_OPEN)) < timedelta(seconds=1)
    called = []
    rep = chain.run_chain("announcements", [("bse_announcements", lambda cs: called.append(1) or ([], []))],
                          cos, chain.write_filings, lambda r: True)
    assert not called and "circuit open" in rep["bse_announcements"]["skipped"]
    # after the open period the source is tried again; one success closes the breaker
    assert chain.breaker_open("bse_announcements", t0 + chain.BREAKER_OPEN + timedelta(minutes=1)) is None
    chain.record("bse_announcements", True, added=1, seen=1)
    h = {x["source"]: x for x in chain.health()}["bse_announcements"]
    assert h["ok"] and h["consecutive_failures"] == 0 and h["circuit_open_until"] is None and h["last_success_at"]


def test_cross_exchange_duplicate_is_kept_but_hidden(cos):
    from finn.database import session
    from finn.models import Filing
    from finn.sources import chain
    chain.write_filings([_row(1, "BSE", 60, subject="Outcome of Board Meeting - results")])
    nse_dup = _row(1, "NSE", 55, subject="Outcome of Board Meeting results")        # 5 min later, same text
    nse_other = _row(1, "NSE", 5, subject="Allotment of ESOP shares")               # a different disclosure
    assert chain.write_filings([nse_dup, nse_other]) == 2                         # nothing is dropped
    with session() as s:
        rows = {f.subject: f for f in s.query(Filing)}
    assert rows["Outcome of Board Meeting results"].status == "duplicate"
    assert "same disclosure as filing" in rows["Outcome of Board Meeting results"].error
    assert rows["Allotment of ESOP shares"].status == "new"


def test_rerun_is_idempotent(cos):
    from finn.sources import chain
    rows = [_row(2, "BSE", 10)]
    assert chain.write_filings([dict(r) for r in rows]) == 1
    assert chain.write_filings([dict(r) for r in rows]) == 0


def test_freshness_amber_only_after_26h_on_trading_days(cos, monkeypatch):
    from finn import ingest
    monkeypatch.setattr(ingest, "announcements_age", lambda: timedelta(hours=30))
    monkeypatch.setattr("finn.fiscal.is_trading_day", lambda d: False)
    assert ingest._freshness()["stale"] is False          # quiet weekend
    monkeypatch.setattr("finn.fiscal.is_trading_day", lambda d: True)
    assert ingest._freshness()["stale"] is True
    monkeypatch.setattr(ingest, "announcements_age", lambda: timedelta(hours=20))
    assert ingest._freshness()["stale"] is False


def test_scheduler_slots():
    from finn.fiscal import IST
    from finn.scheduler import due, last_slot, next_slot
    t = datetime(2026, 11, 16, 17, 0, tzinfo=IST)
    assert last_slot(t).strftime("%H:%M") == "16:30" and next_slot(t).strftime("%H:%M") == "19:00"
    assert last_slot(datetime(2026, 11, 16, 7, 0, tzinfo=IST)).strftime("%d %H:%M") == "15 22:00"
    assert due(t, datetime(2026, 11, 16, 16, 0, tzinfo=IST)) and not due(t, datetime(2026, 11, 16, 16, 45, tzinfo=IST))


def test_tasks_endpoint_requires_token(monkeypatch):
    from fastapi.testclient import TestClient
    import api
    c = TestClient(api.app)
    monkeypatch.delenv("FINN_TASK_TOKEN", raising=False)
    assert c.post("/tasks/ingest").status_code == 503
    monkeypatch.setenv("FINN_TASK_TOKEN", "s3cret")
    assert c.post("/tasks/ingest").status_code == 401
    assert c.post("/tasks/ingest", headers={"X-FINN-Task-Token": "nope"}).status_code == 401
    started = []
    monkeypatch.setattr("finn.ingest.run", lambda **k: started.append(k))
    r = c.post("/tasks/ingest", headers={"X-FINN-Task-Token": "s3cret"})
    assert r.status_code == 202 and r.json()["started"] is True


def test_run_lock_is_exclusive_released_and_expires(sqldb):
    from datetime import timedelta
    from finn import ingest
    with ingest.run_lock() as a:
        assert a is True
        with ingest.run_lock() as b:
            assert b is False                      # a second trigger while a run is going does nothing
    with ingest.run_lock() as c:
        assert c is True                           # released after the run
    with ingest.run_lock(lease=timedelta(seconds=-1)) as d:   # a run that "crashed" with an already-expired lease
        assert d is True
        with ingest.run_lock() as e:
            assert e is True                       # an expired lease can be taken over


def test_overlapping_run_is_skipped(sqldb):
    from finn import ingest
    with ingest.run_lock():
        assert ingest.run(trigger="test", pipeline=False)["skipped"]


def test_dates_in_filing_text():
    from finn.sources.calendar import dates_in_text
    t = ("The company plans a meeting with CITIC CLSA India Forum on 16 November 2026 at 10:00am IST. "
         "Interactions are scheduled for 18 and 19 November 2026 at 08:30am SGT. Board meeting on 3rd Dec, 2026.")
    assert dates_in_text(t) == [date(2026, 11, 16), date(2026, 11, 18), date(2026, 12, 3)]
