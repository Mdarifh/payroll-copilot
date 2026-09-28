from datetime import date

import pytest

from payroll_copilot.core.dates import FinancialYear, PayrollPeriod, date_ranges_overlap
from payroll_copilot.core.exceptions import PeriodError


class TestPayrollPeriod:
    def test_parse_and_properties(self):
        period = PayrollPeriod.parse("2026-09")
        assert (period.year, period.month) == (2026, 9)
        assert period.start == date(2026, 9, 1)
        assert period.end == date(2026, 9, 30)
        assert period.calendar_days == 30
        assert period.label == "Sep-2026"
        assert str(period) == "2026-09"

    @pytest.mark.parametrize(
        ("value", "days"), [("2028-02", 29), ("2026-02", 28), ("2026-07", 31)]
    )
    def test_calendar_days(self, value, days):
        assert PayrollPeriod.parse(value).calendar_days == days

    @pytest.mark.parametrize("value", ["2026-9", "09-2026", "2026-13", "2026-00", "Sep-2026", ""])
    def test_invalid(self, value):
        with pytest.raises(PeriodError):
            PayrollPeriod.parse(value)

    def test_next_previous_across_year(self):
        assert PayrollPeriod(2026, 12).next() == PayrollPeriod(2027, 1)
        assert PayrollPeriod(2027, 1).previous() == PayrollPeriod(2026, 12)
        assert PayrollPeriod(2026, 9).next() == PayrollPeriod(2026, 10)

    def test_contains(self):
        period = PayrollPeriod(2026, 9)
        assert period.contains(date(2026, 9, 30))
        assert not period.contains(date(2026, 10, 1))

    def test_ordering(self):
        assert PayrollPeriod(2026, 8) < PayrollPeriod(2026, 9) < PayrollPeriod(2027, 1)

    @pytest.mark.parametrize(
        ("value", "fy"), [("2026-04", "2026-27"), ("2027-03", "2026-27"), ("2026-03", "2025-26")]
    )
    def test_financial_year(self, value, fy):
        assert PayrollPeriod.parse(value).financial_year.label == fy


class TestFinancialYear:
    def test_parse(self):
        fy = FinancialYear.parse("2026-27")
        assert fy.start == date(2026, 4, 1)
        assert fy.end == date(2027, 3, 31)
        assert str(fy) == "2026-27"

    def test_century_rollover(self):
        assert FinancialYear.parse("2099-00").end == date(2100, 3, 31)

    @pytest.mark.parametrize("value", ["2026-28", "2026", "26-27", "2026/27"])
    def test_invalid(self, value):
        with pytest.raises(PeriodError):
            FinancialYear.parse(value)

    def test_periods(self):
        periods = FinancialYear(2026).periods()
        assert len(periods) == 12
        assert periods[0] == PayrollPeriod(2026, 4)
        assert periods[-1] == PayrollPeriod(2027, 3)

    @pytest.mark.parametrize(("value", "left"), [("2026-04", 12), ("2026-09", 7), ("2027-03", 1)])
    def test_months_remaining(self, value, left):
        assert FinancialYear(2026).months_remaining(PayrollPeriod.parse(value)) == left

    def test_months_remaining_outside_fy(self):
        with pytest.raises(PeriodError):
            FinancialYear(2026).months_remaining(PayrollPeriod(2027, 4))


class TestDateRangesOverlap:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ((date(2014, 9, 1), date(2026, 8, 31)), (date(2026, 9, 1), None), False),
            ((date(2014, 9, 1), None), (date(2026, 9, 1), date(2026, 9, 30)), True),
            ((date(2026, 9, 1), date(2026, 9, 1)), (date(2026, 9, 1), date(2026, 9, 30)), True),
            ((date(2026, 1, 1), None), (date(2025, 1, 1), None), True),
        ],
    )
    def test_overlap(self, a, b, expected):
        assert date_ranges_overlap(*a, *b) is expected
