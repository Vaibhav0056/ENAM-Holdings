from datetime import date, datetime, timezone

import pytest

from finn import fiscal as f
from finn.fiscal import Period


@pytest.mark.parametrize("d,fy,q", [
    (date(2025, 4, 1), 2026, 1), (date(2025, 6, 30), 2026, 1), (date(2025, 7, 1), 2026, 2),
    (date(2025, 9, 30), 2026, 2), (date(2025, 12, 31), 2026, 3), (date(2026, 1, 1), 2026, 4),
    (date(2026, 3, 31), 2026, 4), (date(2026, 4, 1), 2027, 1),
])
def test_quarter_of(d, fy, q):
    assert f.quarter_of(d) == Period(fy, q)


def test_q2_fy26_is_jul_sep_2025():
    p = Period(2026, 2)
    assert (p.start(), p.end(), p.label()) == (date(2025, 7, 1), date(2025, 9, 30), "Q2 FY26")


def test_year_half_nine_month_bounds():
    assert (Period(2026).start(), Period(2026).end()) == (date(2025, 4, 1), date(2026, 3, 31))
    assert Period(2026, h=2).start() == date(2025, 10, 1)
    assert Period(2026, m9=True).end() == date(2025, 12, 31)


def test_prev_and_year_ago():
    assert f.prev_quarter(Period(2026, 1)) == Period(2025, 4)
    assert f.prev_quarter(Period(2026, 3)) == Period(2026, 2)
    assert f.year_ago(Period(2026, 2)) == Period(2025, 2)
    assert f.last_completed_quarter(date(2026, 11, 11)) == Period(2027, 2)


@pytest.mark.parametrize("text,expected", [
    ("Q2 FY26", Period(2026, 2)), ("Q2FY26", Period(2026, 2)), ("2QFY26", Period(2026, 2)),
    ("Q2 FY2026", Period(2026, 2)), ("FY2025-26", Period(2026)), ("2025-26", Period(2026)),
    ("FY 2025-2026", Period(2026)), ("FY26", Period(2026)), ("FY'27", Period(2027)),
    ("H1 FY26", Period(2026, h=1)), ("9M FY26", Period(2026, m9=True)),
    ("quarter ended 30 September 2025", Period(2026, 2)), ("Sep 2025 quarter", Period(2026, 2)),
    ("quarter ended December 31, 2025", Period(2026, 3)), ("year ended 31st March 2026", Period(2026)),
    ("half year ended September 30, 2025", Period(2026, h=1)),
    ("third quarter FY26", Period(2026)),  # ordinal words are not parsed: the year is still right
    ("3rd quarter of FY26", Period(2026, 3)),
])
def test_parse_period(text, expected):
    assert f.parse_period(text) == expected


@pytest.mark.parametrize("text", ["medium term", "next few years", "", None, "by 2030 sometime"])
def test_parse_period_unknown(text):
    assert f.parse_period(text) is None


def test_period_ordering_by_end_date():
    assert Period(2026, 2) < Period(2026, 3) < Period(2026) < Period(2027, 1)
    assert max([Period(2025), Period(2026, 1), Period(2025, 4)]) == Period(2026, 1)


@pytest.mark.parametrize("value,unit,crore", [
    (1234.5, "₹ in crore", 1234.5), (123450, "Rs. in Lakhs", 1234.5), (12345, "INR million", 1234.5),
    (1.2345, "₹ billion", 123.45), (12345000, "₹ '000", 1234.5), (12345000000, "Rs", 1234.5),
    (5, "(₹ Cr)", 5),
])
def test_to_crore(value, unit, crore):
    assert f.to_crore(value, unit) == pytest.approx(crore)


def test_unknown_unit_is_none():
    assert f.to_crore(10, "units") is None and f.to_crore(None, "crore") is None


def test_ist_utc_round_trip():
    naive_ist = datetime(2026, 11, 9, 16, 59, 25)
    utc = f.to_utc(naive_ist)
    assert utc == datetime(2026, 11, 9, 11, 29, 25, tzinfo=timezone.utc)
    assert f.to_ist(utc).replace(tzinfo=None) == naive_ist
    start, end = f.ist_day_bounds(date(2026, 11, 9))
    assert start == datetime(2026, 11, 8, 18, 30, tzinfo=timezone.utc) and (end - start).days == 1


def test_trading_days():
    assert not f.is_trading_day(date(2026, 11, 8))           # Sunday
    assert not f.is_trading_day(date(2026, 11, 10))          # Diwali Balipratipada
    assert f.next_trading_day(date(2026, 11, 9)) == date(2026, 11, 11)
    assert f.prev_trading_day(date(2026, 11, 11)) == date(2026, 11, 9)


def test_first_session_after_filing():
    before_close = f.to_utc(datetime(2026, 11, 9, 14, 0))
    after_close = f.to_utc(datetime(2026, 11, 9, 16, 59))
    assert f.first_session_after(before_close) == date(2026, 11, 9)
    assert f.first_session_after(after_close) == date(2026, 11, 11)   # skips the 10 Nov holiday
