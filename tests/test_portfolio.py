"""Portfolio editing: validation, add with background onboarding, removal that keeps history, and a re-seed that
never undoes a change made in Settings. Network calls are stubbed."""
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def pf(sqldb, monkeypatch):
    from finn import portfolio
    portfolio.JOBS.clear()
    monkeypatch.setattr(portfolio, "nse_listing", lambda s: {"symbol": s, "name": f"{s} India Ltd", "isin": "INE000X01010"}
                        if s in ("HAVELLS", "TCS") else None)
    monkeypatch.setattr(portfolio, "bse_code_for", lambda isin: "517354")
    monkeypatch.setattr("finn.sources.prices.fetch", lambda sym, days: None)
    monkeypatch.setattr("finn.sources.prices.frame_to_rows", lambda df, sym, cid: [{"close": 1000.0}])
    onboarded = []
    monkeypatch.setattr(portfolio, "_onboard", lambda sym: (onboarded.append(sym), portfolio.JOBS[sym].update(done=True)))
    monkeypatch.setattr("threading.Thread.start", lambda self: self._target(*self._args))
    return portfolio, onboarded


def test_invalid_tickers_are_rejected_with_a_reason(pf):
    portfolio, onboarded = pf
    job = portfolio.add("NOTREAL")
    assert job["done"] and "not on NSE's list" in job["error"] and onboarded == []
    assert "Enter an NSE symbol" in portfolio.add("bad symbol!")["error"]       # rejected before any network call


def test_add_saves_the_holding_and_starts_onboarding(pf):
    from finn.database import session
    from finn.models import Company
    portfolio, onboarded = pf
    job = portfolio.add("havells")
    assert job["error"] is None and onboarded == ["HAVELLS"] and "BSE 517354" in job["log"][0]
    with session() as s:
        c = s.query(Company).filter_by(nse_symbol="HAVELLS").one()
        assert c.is_portfolio and c.status == "ok" and c.yahoo_symbol == "HAVELLS.NS" and c.bse_code == "517354"


def test_remove_hides_but_keeps_history_and_add_restores(pf):
    from finn.database import session
    from finn.models import Company, Filing
    portfolio, _ = pf
    portfolio.add("TCS")
    with session() as s:
        cid = s.query(Company).filter_by(nse_symbol="TCS").one().id
        s.add(Filing(company_id=cid, exchange="BSE", exchange_id="1", subject="Results", content_hash="a" * 64,
                     filed_at=datetime(2026, 10, 9, tzinfo=timezone.utc)))
    portfolio.remove("TCS")
    with session() as s:
        c = s.get(Company, cid)
        assert not c.is_portfolio and c.status == "removed" and s.query(Filing).filter_by(company_id=cid).count() == 1
    job = portfolio.add("TCS")
    assert "Restored" in job["log"][0]
    with session() as s:
        assert s.get(Company, cid).is_portfolio


def test_reseeding_never_undoes_a_settings_change(sqldb, tmp_path):
    from finn import seed
    from finn.database import session
    from finn.models import Company
    csv = tmp_path / "companies.csv"
    csv.write_text("nse_symbol,bse_code,yahoo_symbol,isin,name,sector,industry,is_portfolio,peer_of,status,note\n"
                   "TCS,532540,TCS.NS,INE467B01029,TCS,IT,IT,True,,ok,\n"
                   "HAVELLS,517354,HAVELLS.NS,INE176B01034,Havells,Elec,Elec,False,,ok,\n", encoding="utf-8")
    seed.seed_companies(csv)
    with session() as s:
        s.query(Company).filter_by(nse_symbol="TCS").one().is_portfolio = False        # removed in Settings
        s.query(Company).filter_by(nse_symbol="HAVELLS").one().is_portfolio = True     # added in Settings
    seed.seed_companies(csv)                                                           # the next deploy re-seeds
    with session() as s:
        assert not s.query(Company).filter_by(nse_symbol="TCS").one().is_portfolio
        assert s.query(Company).filter_by(nse_symbol="HAVELLS").one().is_portfolio


def test_ingest_skips_removed_companies(sqldb):
    from finn import ingest
    from finn.database import session
    from finn.models import Company
    with session() as s:
        s.add_all([Company(nse_symbol="AAA", name="A", is_portfolio=True, status="ok"),
                   Company(nse_symbol="BBB", name="B", is_portfolio=False, status="removed")])
    assert [c.nse_symbol for c in ingest.companies()] == ["AAA"]



def test_removing_during_onboarding_stops_it(sqldb, monkeypatch):
    """Run 1 of the golden path: a research peer added, then removed while its ticker was still being checked; the
    check finished afterwards and made it a holding again."""
    from finn import portfolio
    from finn.database import session
    from finn.models import Company
    portfolio.JOBS.clear()
    with session() as s:
        s.add(Company(nse_symbol="HAVELLS", name="Havells", is_portfolio=False, status="ok"))

    def validate_while_user_removes(sym):
        portfolio.remove(sym)                         # the user taps remove before the check returns
        return {"symbol": sym, "name": "Havells", "isin": "INE0", "yahoo_symbol": f"{sym}.NS", "bse_code": None}
    monkeypatch.setattr(portfolio, "validate", validate_while_user_removes)
    monkeypatch.setattr("threading.Thread.start", lambda self: self._target(*self._args))
    job = portfolio.add("HAVELLS")
    assert job["done"] and "Removed" in job["error"]
    with session() as s:
        c = s.query(Company).filter_by(nse_symbol="HAVELLS").one()
        assert not c.is_portfolio and c.status == "removed"     # stays removed


def test_feature_flags_defaults_and_overrides(monkeypatch):
    from finn import flags
    monkeypatch.delenv("FINN_FLAGS", raising=False)
    f = flags.all_flags()
    assert f["charts"] and f["research"] and not f["web_push"] and not f["audio_brief"]
    monkeypatch.setenv("FINN_FLAGS", "web_push=1, copilot=0, nonsense=1")
    f = flags.all_flags()
    assert f["web_push"] and not f["copilot"] and "nonsense" not in f
    assert {r["feature"] for r in flags.table()} == set(flags.FEATURES)



def test_removing_a_research_peer_keeps_it_tracked(sqldb):
    from finn import portfolio
    from finn.database import session
    from finn.models import Company
    with session() as s:
        s.add(Company(id=1, nse_symbol="POLYCAB", name="Polycab", is_portfolio=True))
        s.add(Company(id=2, nse_symbol="HAVELLS", name="Havells", is_portfolio=True, peer_of=1))
    portfolio.remove("HAVELLS")
    with session() as s:
        h = s.get(Company, 2)
        assert not h.is_portfolio and h.status == "ok"          # still ingested as POLYCAB's peer
