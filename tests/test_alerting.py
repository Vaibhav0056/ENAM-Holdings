"""Phase 6: volume burst cooldown, ex-date suppression, promise diff, database-level dedupe."""
from datetime import date, timedelta

from finn.alerting import engine as A

D0 = date(2026, 9, 1)


def rows(volumes, closes=None):
    closes = closes or [100.0] * len(volumes)
    return [{"date": D0 + timedelta(days=i), "volume": v, "close": c} for i, (v, c) in enumerate(zip(volumes, closes))]


def test_volume_spike_alerts_once_per_burst_unless_it_escalates():
    vols = [100.0] * 25 + [300, 250, 280, 500]
    hits = A.volume_hits(rows(vols), 2.0, D0)
    assert [h["ratio"] for h in hits] == [3.0, 5.0]


def test_price_move_is_suppressed_on_ex_dates():
    closes = [100.0] * 30 + [93.0]
    r = rows([1.0] * 31, closes)
    assert len(A.move_hits(r, 5.0, {}, D0, set())) == 1
    assert A.move_hits(r, 5.0, {}, D0, {r[-1]["date"]}) == []


def test_promise_diff_new_and_dropped():
    old = [{"metric": "EBITDA margin", "target_low": 11, "target_high": 13, "unit": "%", "horizon": "FY27",
            "also_said_in": ["Q4 FY26"]},
           {"metric": "capex", "target_low": 100, "target_high": 100, "unit": "crore", "horizon": "FY27"}]
    new = [{"metric": "EBITDA margins", "target_low": 11, "target_high": 13, "unit": "%", "horizon": "FY27"},
           {"metric": "export share", "target_low": 10, "target_high": None, "unit": "%", "horizon": "FY30"}]
    added, dropped = A.promise_diff(old, new)
    assert [p["metric"] for p in added] == ["export share"]
    assert dropped == []                      # capex was said once only: not repeated, so not "dropped"
    added, dropped = A.promise_diff(old, new[1:])
    assert [p["metric"] for p in dropped] == ["EBITDA margin"]


def test_alert_dedupe_is_enforced_by_the_database(sqldb):
    from finn.database import session
    from finn.models import AlertRule, Company
    with session() as s:
        s.add(Company(id=1, nse_symbol="TCS", name="TCS", is_portfolio=True))
        s.add(AlertRule(id=1, kind="price_move", label="x", params={"pct": 5}))
    assert A._insert(1, 1, D0, "TCS down 6%", "line", "/#holding/TCS") is True
    assert A._insert(1, 1, D0, "TCS down 6%", "line", "/#holding/TCS") is False
