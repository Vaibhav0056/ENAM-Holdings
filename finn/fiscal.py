"""Indian fiscal calendar, IST/UTC handling, NSE trading days and unit normalisation.

Indian FY runs April to March and is named by the year it ends in: FY26 = Apr 2025 .. Mar 2026.
Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar. "Q2 FY26" is Jul-Sep 2025.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import total_ordering
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ---------- time zones: store UTC, display IST ----------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime, assume=IST) -> datetime:
    """Naive datetimes from Indian sources (BSE DissemDT) are IST wall-clock time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume)
    return dt.astimezone(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def ist_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[start, end) of an IST calendar day, expressed in UTC."""
    start = datetime(d.year, d.month, d.day, tzinfo=IST)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


# ---------- fiscal periods ----------


@total_ordering
@dataclass(frozen=True)
class Period:
    """fy = fiscal year number (2026 for FY26). q = 1..4 for a quarter; h = 1/2 for a half year;
    m9 = True for nine months; none of them for the full year."""
    fy: int
    q: int | None = None
    h: int | None = None
    m9: bool = False

    @property
    def kind(self) -> str:
        return "quarter" if self.q else "half" if self.h else "nine_months" if self.m9 else "year"

    @property
    def months(self) -> int:
        return {"quarter": 3, "half": 6, "nine_months": 9, "year": 12}[self.kind]

    def start(self) -> date:
        first = date(self.fy - 1, 4, 1)
        if self.q:
            return _add_months(first, 3 * (self.q - 1))
        if self.h:
            return _add_months(first, 6 * (self.h - 1))
        return first

    def end(self) -> date:
        """Last calendar day of the period."""
        return _add_months(self.start(), self.months) - timedelta(days=1)

    def label(self) -> str:
        fy = f"FY{self.fy % 100:02d}"
        if self.q:
            return f"Q{self.q} {fy}"
        if self.h:
            return f"H{self.h} {fy}"
        if self.m9:
            return f"9M {fy}"
        return fy

    def __lt__(self, other: "Period") -> bool:  # ordered by end date, then by length
        return (self.end(), self.months) < (other.end(), other.months)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def fy_of(d: date) -> int:
    return d.year + 1 if d.month >= 4 else d.year


def quarter_of(d: date) -> Period:
    return Period(fy_of(d), ((d.month - 4) % 12) // 3 + 1)


def prev_quarter(p: Period) -> Period:
    if not p.q:
        raise ValueError("prev_quarter needs a quarter")
    return Period(p.fy, p.q - 1) if p.q > 1 else Period(p.fy - 1, 4)


def year_ago(p: Period) -> Period:
    return Period(p.fy - 1, p.q, p.h, p.m9)


def last_completed_quarter(today: date) -> Period:
    return prev_quarter(quarter_of(today))


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _year(s: str) -> int:
    n = int(s)
    return 2000 + n if n < 100 else n


def parse_period(text: str | None) -> Period | None:
    """Parse the ways Indian filings and calls name a period. Returns None when the text can't be pinned
    to a period (e.g. "medium term"); callers treat that as unverifiable rather than guessing."""
    if not text:
        return None
    t = text.lower().replace("–", "-").replace("—", "-").replace("’", "'")
    # FY2025-26, 2025-26, FY 2025-2026 -> fy26 (the year the fiscal year ends in)
    t = re.sub(r"(?:fy\s*'?\s*)?20(\d{2})\s*[-/]\s*(?:20)?(\d{2})\b", lambda m: f"fy{m.group(2)}", t)
    t = re.sub(r"fy\s*'?\s*(\d{4}|\d{2})\b", lambda m: f"fy{_year(m.group(1)) % 100:02d}", t)
    t = re.sub(r"\s+", " ", t)
    fy = re.search(r"fy(\d{2})\b", t)
    if fy:
        y = 2000 + int(fy.group(1))
        m = re.search(r"\bq\s*([1-4])|\b([1-4])\s*q(?:\b|(?=fy))|\b([1-4])(?:st|nd|rd|th) quarter", t)
        if m:
            return Period(y, int(next(g for g in m.groups() if g)))
        m = re.search(r"\bh\s*([12])\b|\b([12])\s*h\b", t)
        if m:
            return Period(y, h=int(next(g for g in m.groups() if g)))
        if re.search(r"\b9\s*m\b|nine months", t):
            return Period(y, m9=True)
        return Period(y)
    # "quarter ended 30 September 2025", "Sep 2025 quarter", "September 30, 2025"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*(?:\d{1,2},?\s*)?'?(\d{4}|\d{2})\b", t)
    if not m:
        m2 = re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*,?\s*(\d{4})\b", t)
        m = m2
    if m:
        month, year = _MONTHS[m.group(1)], _year(m.group(2))
        end = date(year, month, 1)
        if re.search(r"half year|six months", t):
            return Period(fy_of(end), h=1 if month == 9 else 2) if month in (9, 3) else None
        if re.search(r"nine months", t):
            return Period(fy_of(end), m9=True) if month == 12 else None
        if re.search(r"year ended|financial year|fiscal year", t):
            return Period(fy_of(end)) if month == 3 else None
        if month in (3, 6, 9, 12):
            return quarter_of(end)
    return None


# ---------- units: everything shown in ₹ crore ----------

UNIT_TO_CRORE = {
    "crore": 1.0, "crores": 1.0, "cr": 1.0,
    "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
    "million": 0.1, "millions": 0.1, "mn": 0.1,
    "billion": 100.0, "billions": 100.0, "bn": 100.0,
    "thousand": 1e-4, "thousands": 1e-4, "'000": 1e-4,
}


def unit_factor(unit: str | None) -> float | None:
    """Multiplier from a reported unit to ₹ crore. None when the unit isn't recognised (never guess)."""
    if not unit:
        return None
    u = unit.lower()
    for key in sorted(UNIT_TO_CRORE, key=len, reverse=True):
        if re.search(rf"(?<![a-z']){re.escape(key)}(?![a-z])", u):
            return UNIT_TO_CRORE[key]
    if re.fullmatch(r"\W*(rs|inr|rupees|₹)\W*", u):
        return 1e-7
    return None


def to_crore(value: float | None, unit: str | None) -> float | None:
    f = unit_factor(unit)
    if value is None or f is None:
        return None
    return round(value * f, 4)


# ---------- NSE trading days ----------

# NSE equity trading holidays that fall on weekdays. Cross-checked against the dates missing from the
# ^NSEI daily bars in the prices table (tests/test_fiscal.py and the Phase 1 log).
NSE_HOLIDAYS = {
    # 2025
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31), date(2025, 4, 10), date(2025, 4, 14),
    date(2025, 4, 18), date(2025, 5, 1), date(2025, 8, 15), date(2025, 8, 27), date(2025, 10, 2),
    date(2025, 10, 21), date(2025, 10, 22), date(2025, 11, 5), date(2025, 12, 25),
    # 2026
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28), date(2026, 6, 26), date(2026, 9, 14),
    date(2026, 10, 2), date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def first_session_after(filed_at: datetime) -> date:
    """The first NSE session that could react to a filing: the same day if filed before the 15:30 IST
    close on a trading day, otherwise the next trading day."""
    local = to_ist(filed_at)
    d = local.date()
    if is_trading_day(d) and (local.hour, local.minute) < (15, 30):
        return d
    return next_trading_day(d)
