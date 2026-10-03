"""Payroll period and financial-year helpers (Indian FY: 1 April - 31 March)."""

import calendar
import re
from dataclasses import dataclass
from datetime import date

from payroll_copilot.core.exceptions import PeriodError

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")
_FY_RE = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True, order=True)
class PayrollPeriod:
    """A payroll month, written as 'YYYY-MM'."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise PeriodError(f"Invalid month: {self.month}")
        if not 1900 <= self.year <= 9999:
            raise PeriodError(f"Invalid year: {self.year}")

    @classmethod
    def parse(cls, value: str) -> "PayrollPeriod":
        match = _PERIOD_RE.match(value.strip())
        if not match:
            raise PeriodError(f"Payroll period must be 'YYYY-MM', got {value!r}")
        return cls(int(match.group(1)), int(match.group(2)))

    @classmethod
    def from_date(cls, value: date) -> "PayrollPeriod":
        return cls(value.year, value.month)

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        return date(self.year, self.month, self.calendar_days)

    @property
    def calendar_days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def label(self) -> str:
        """Human label, e.g. 'Sep-2026'."""
        return f"{calendar.month_abbr[self.month]}-{self.year}"

    @property
    def financial_year(self) -> "FinancialYear":
        return FinancialYear(self.year if self.month >= 4 else self.year - 1)

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end

    def next(self) -> "PayrollPeriod":
        return PayrollPeriod(self.year + self.month // 12, self.month % 12 + 1)

    def previous(self) -> "PayrollPeriod":
        if self.month == 1:
            return PayrollPeriod(self.year - 1, 12)
        return PayrollPeriod(self.year, self.month - 1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, order=True)
class FinancialYear:
    """Indian financial year, written as '2026-27' (1 Apr 2026 - 31 Mar 2027)."""

    start_year: int

    @classmethod
    def parse(cls, value: str) -> "FinancialYear":
        match = _FY_RE.match(value.strip())
        if not match:
            raise PeriodError(f"Financial year must be 'YYYY-YY', got {value!r}")
        start_year = int(match.group(1))
        if (start_year + 1) % 100 != int(match.group(2)):
            raise PeriodError(f"Financial year end does not follow start: {value!r}")
        return cls(start_year)

    @classmethod
    def from_date(cls, value: date) -> "FinancialYear":
        return PayrollPeriod.from_date(value).financial_year

    @property
    def start(self) -> date:
        return date(self.start_year, 4, 1)

    @property
    def end(self) -> date:
        return date(self.start_year + 1, 3, 31)

    @property
    def label(self) -> str:
        return f"{self.start_year}-{(self.start_year + 1) % 100:02d}"

    def periods(self) -> list[PayrollPeriod]:
        """All 12 payroll months, April to March."""
        first = PayrollPeriod(self.start_year, 4)
        result = [first]
        while len(result) < 12:
            result.append(result[-1].next())
        return result

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end

    def months_remaining(self, period: PayrollPeriod) -> int:
        """Months left in the FY including ``period`` itself (April -> 12, March -> 1)."""
        if period.financial_year != self:
            raise PeriodError(f"{period} is not in financial year {self.label}")
        return 12 - self.periods().index(period)

    def __str__(self) -> str:
        return self.label


def date_ranges_overlap(
    a_start: date, a_end: date | None, b_start: date, b_end: date | None
) -> bool:
    """True if two inclusive date ranges overlap. ``None`` end means open-ended."""
    a_end = a_end or date.max
    b_end = b_end or date.max
    return a_start <= b_end and b_start <= a_end


_MONTH_NAMES = {name.lower(): i for i, name in enumerate(calendar.month_name) if name} | {
    name.lower(): i for i, name in enumerate(calendar.month_abbr) if name
} | {"sept": 9}
_LOOSE_PERIOD = [
    (re.compile(r"^(\d{4})[-/ ](\d{1,2})$"), "ym"),
    (re.compile(r"^(\d{1,2})[-/ ](\d{4})$"), "my"),
    (re.compile(r"^([a-z]+)\.?[-,' ]*(\d{4})$"), "named"),
]


def parse_month(text: str) -> PayrollPeriod:
    """Payroll month from 'YYYY-MM', '2026/9', '09-2026', 'Sep 2026', 'September, 2026'."""
    value = text.strip().lower()
    for pattern, order in _LOOSE_PERIOD:
        match = pattern.match(value)
        if not match:
            continue
        a, b = match.groups()
        if order == "named":
            if a not in _MONTH_NAMES:
                break
            return PayrollPeriod(int(b), _MONTH_NAMES[a])
        year, month = (a, b) if order == "ym" else (b, a)
        return PayrollPeriod(int(year), int(month))
    raise PeriodError(f"Month must look like YYYY-MM (e.g. 2026-09), got {text!r}")


def parse_financial_year(text: str) -> FinancialYear:
    """'2026-27', 'FY 2026-27', '2026-2027' or a start year '2026'."""
    value = text.strip().upper().removeprefix("FY").strip()
    if re.fullmatch(r"\d{4}", value):
        return FinancialYear(int(value))
    match = re.fullmatch(r"(\d{4})-(\d{4})", value)
    if match:
        start, end = map(int, match.groups())
        if end != start + 1:
            raise PeriodError(f"Financial year end does not follow start: {text!r}")
        return FinancialYear(start)
    return FinancialYear.parse(value)
