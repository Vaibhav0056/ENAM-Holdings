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
    # baseline = mean of the 20 sessions before: 100, then 110, 117.5, 126.5 as the burst enters it
    assert [h["ratio"] for h in hits] == [3.0, 3.95]


def test_volume_rule_default_3x_and_liquidity_floor():
    vols = [1_000_000.0] * 25 + [2_500_000, 3_400_000]
    r = rows(vols, [100.0] * len(vols))            # ₹100 × 10 lakh shares = ₹10 crore traded a day
    # day 26: 2.5× (not a hit); day 27: 3.4M vs an average of 1.075M = 3.16×, on ₹10.75 crore traded a day
    assert [h["ratio"] for h in A.volume_hits(r, 3.0, D0)] == [3.16]
    assert len(A.volume_hits(r, 3.0, D0, min_value_cr=10.0)) == 1
    assert A.volume_hits(r, 3.0, D0, min_value_cr=25.0) == []   # too thinly traded for this floor


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


def test_one_alert_per_company_per_day(sqldb):
    from finn.database import session
    from finn.models import Alert, AlertRule, Company
    with session() as s:
        s.add(Company(id=1, nse_symbol="AAA", name="AAA", is_portfolio=True))
        for i, kind in enumerate(["volume_spike", "price_move", "announcement_type"], 1):
            s.add(AlertRule(id=i, kind=kind, label=kind, params={}, enabled=True))
    d = date(2026, 11, 13)
    assert A._insert(1, 1, d, "AAA: volume 3.4× its 20-day average", "34 lakh shares", "/#holding/AAA")
    assert A._insert(2, 1, d, "AAA down 6.1%", "closed ₹94", "/#holding/AAA")
    assert A._insert(3, 1, d, "AAA: Results", "Revenue +12.0% YoY", "/#card/5")
    assert A._insert(1, 1, d + timedelta(days=1), "AAA: volume 3.1× its 20-day average", "31 lakh", "/#holding/AAA")
    out = A.consolidate()
    assert out == {"groups_merged": 1, "superseded": 2}
    with session() as s:
        active = s.query(Alert).filter(Alert.status == "active").order_by(Alert.triggered_on).all()
        assert len(active) == 2                                  # one per company per day
        top = active[0]
        assert top.title == "AAA: Results"                        # the filing outranks price and volume
        assert {x["kind"] for x in top.detail["also"]} == {"price_move", "volume_spike"}
        assert " · also: " in top.line
        assert s.query(Alert).filter(Alert.status == "superseded", Alert.superseded_by == top.id).count() == 2
    assert A.consolidate() == {"groups_merged": 0, "superseded": 0}   # idempotent


def test_reevaluate_is_repeatable(sqldb):
    from finn.database import session
    from finn.models import Alert, AlertRule, Company
    with session() as s:
        s.add(Company(id=1, nse_symbol="AAA", name="AAA", is_portfolio=True))
        s.add(AlertRule(id=1, kind="volume_spike", label="v", params={"multiple": 3.0, "min_avg_value_cr": 10.0}, enabled=True))
        s.add(AlertRule(id=2, kind="announcement_type", label="f", params={"types": ["results"]}, enabled=True))
    d = date(2026, 11, 13)
    A._insert(1, 1, d, "AAA: volume 2.1× its 20-day median", "x", "/#holding/AAA")   # no prices: no longer a hit
    A._insert(2, 1, d, "AAA: Results", "Revenue +12.0% YoY", "/#card/5")
    first = A.reevaluate()
    second = A.reevaluate()
    assert first["volume_superseded"] == 1 and second["volume_superseded"] == 0
    assert first["active"] == second["active"] == 1
    with session() as s:
        top = s.query(Alert).filter(Alert.status == "active").one()
        assert top.title == "AAA: Results" and " · also: " not in top.line
