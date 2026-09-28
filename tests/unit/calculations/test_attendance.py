from datetime import date, timedelta
from decimal import Decimal

import pytest

from payroll_copilot.calculations.attendance import (
    compute_paid_days,
    diagnose_paid_days,
    employed_span,
)
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import AttendanceError
from payroll_copilot.domain.attendance import AttendanceRecord
from payroll_copilot.domain.enums import ComparisonStatus, PaidDaysBasis

D = Decimal
SEP = PayrollPeriod(2026, 9)
OLD_DOJ = date(2020, 1, 1)
CAL, F30, F26, WD = (
    PaidDaysBasis.CALENDAR, PaidDaysBasis.FIXED_30, PaidDaysBasis.FIXED_26,
    PaidDaysBasis.WORKING_DAYS,
)  # fmt: skip


def rec(period="2026-09", **kw) -> AttendanceRecord:
    """September 2026 full month by default: 26 working days, 4 Sunday week offs."""
    p = PayrollPeriod.parse(period)
    values = dict(calendar_days=p.calendar_days, working_days=26, week_offs=4, holidays=0,
                  present_days=26)  # fmt: skip
    values.update(kw)
    return AttendanceRecord(period=period, **{k: D(str(v)) if k != "calendar_days" else v
                                              for k, v in values.items()})  # fmt: skip


def paid(record, doj=OLD_DOJ, lwd=None, basis=CAL, **kw):
    return compute_paid_days(record, doj=doj, lwd=lwd, basis=basis, **kw)


class TestCalendarBasis:
    def test_full_month(self):
        r = paid(rec())
        assert r.value == D(30)
        assert r.breakdown["basis_days"] == D(30)
        assert r.unit == "DAYS"
        assert r.warnings == []

    def test_lop(self):
        r = paid(rec(present_days=23, absent_days=3, lop_days=3))
        assert r.value == D(27)
        assert "30 − LOP 3" in r.to_text()

    def test_half_day_lop(self):
        assert paid(rec(present_days="24.5", absent_days="1.5", lop_days="1.5")).value == D("28.5")

    def test_mid_month_joiner(self):
        r = paid(rec(working_days=14, week_offs=2, present_days=14), doj=date(2026, 9, 15))
        assert r.value == D(16)
        assert r.breakdown["days_before_doj"] == D(14)

    def test_mid_month_leaver(self):
        r = paid(rec(working_days=17, week_offs=3, present_days=17), lwd=date(2026, 9, 20))
        assert r.value == D(20)
        assert r.breakdown["days_after_lwd"] == D(10)

    def test_join_and_leave_same_month(self):
        r = paid(
            rec(working_days=5, week_offs=1, present_days=5),
            doj=date(2026, 9, 8), lwd=date(2026, 9, 13),
        )  # fmt: skip
        assert r.value == D(6)

    def test_single_day(self):
        r = paid(rec(working_days=1, week_offs=0, present_days=1),
                 doj=date(2026, 9, 30), lwd=date(2026, 9, 30))  # fmt: skip
        assert r.value == D(1)

    @pytest.mark.parametrize(("doj", "lwd"), [(date(2026, 10, 1), None), (OLD_DOJ, date(2026, 8, 31))])
    def test_not_employed_in_month(self, doj, lwd):
        r = paid(rec(working_days=0, week_offs=0, present_days=0), doj=doj, lwd=lwd)
        assert r.value == 0
        assert any("Not employed" in w for w in r.warnings)

    def test_leap_february(self):
        feb = rec("2028-02", working_days=25, week_offs=4, present_days=25)
        assert paid(feb).value == D(29)

    @pytest.mark.parametrize("day", range(1, 31))
    def test_paid_days_never_exceed_basis_for_any_doj(self, day):
        doj = date(2026, 9, day)
        employed = 30 - day + 1
        sundays = sum(1 for n in range(employed) if (doj + timedelta(n)).weekday() == 6)
        r = paid(rec(working_days=employed - sundays, week_offs=sundays,
                     present_days=employed - sundays), doj=doj)  # fmt: skip
        assert r.value == D(employed) <= r.breakdown["basis_days"]
        assert r.warnings == []


class TestFixedAndWorkingDaysBasis:
    @pytest.mark.parametrize(("period", "wd", "wo"), [("2026-09", 26, 4), ("2026-07", 27, 4), ("2026-02", 24, 4)])
    def test_fixed_30_full_month_is_always_30(self, period, wd, wo):
        r = paid(rec(period, working_days=wd, week_offs=wo, present_days=wd), basis=F30)
        assert r.value == D(30)

    def test_fixed_26_with_lop(self):
        r = paid(rec(present_days=24, absent_days=2, lop_days=2), basis=F26)
        assert (r.value, r.breakdown["basis_days"]) == (D(24), D(26))

    def test_fixed_30_partial_month_is_prorated_with_warning(self):
        july = rec("2026-07", working_days=13, week_offs=2, present_days=13)
        r = paid(july, doj=date(2026, 7, 17), basis=F30)  # 15 employed days of 31
        assert r.value == D("14.52")  # 30 × 15 / 31
        assert "confirm this matches company policy" in r.warnings[0]

    def test_working_days_full_month(self):
        r = paid(rec(present_days=25, absent_days=1, lop_days=1), basis=WD)
        assert (r.value, r.breakdown["basis_days"]) == (D(25), D(26))

    def test_working_days_partial_month_needs_calendar(self):
        joiner = rec(working_days=14, week_offs=2, present_days=14)
        with pytest.raises(AttendanceError, match="month_working_days"):
            paid(joiner, doj=date(2026, 9, 15), basis=WD)
        r = paid(joiner, doj=date(2026, 9, 15), basis=WD, month_working_days=D(26))
        assert (r.value, r.breakdown["basis_days"]) == (D(14), D(26))


class TestValidation:
    def test_wrong_calendar_days(self):
        with pytest.raises(AttendanceError, match="has 30"):
            paid(rec(calendar_days=31))

    def test_negative_count(self):
        with pytest.raises(AttendanceError, match="cannot be negative"):
            paid(rec(lop_days=-1))

    def test_lop_beyond_payable_days(self):
        with pytest.raises(AttendanceError, match="exceed payable days"):
            paid(rec(working_days=14, week_offs=2, present_days=0, absent_days=14, lop_days=17),
                 doj=date(2026, 9, 15))  # fmt: skip

    def test_period_mismatch(self):
        from payroll_copilot.calculations.attendance import validate_attendance

        with pytest.raises(AttendanceError, match="not 2026-10"):
            validate_attendance(rec(), PayrollPeriod(2026, 10), OLD_DOJ, None)

    @pytest.mark.parametrize(
        ("kw", "message"),
        [
            ({"working_days": 25}, "employed 30 days"),
            ({"present_days": 20}, "present + paid leave + absent = 20"),
            ({"present_days": 25, "absent_days": 1, "lop_days": 2}, "exceed absent days"),
        ],
    )
    def test_inconsistent_data_warns(self, kw, message):
        r = paid(rec(**kw))
        assert any(message in w for w in r.warnings)

    def test_lop_reversal_reported_not_added(self):
        r = paid(rec(present_days=25, absent_days=1, lop_days=1, lop_reversal_days=1))
        assert r.value == D(29)
        assert r.breakdown["lop_reversal_days"] == D(1)
        assert "not added to this month's paid days" in r.to_text()

    def test_employed_span(self):
        assert employed_span(SEP, date(2026, 9, 15), None) == (date(2026, 9, 15), SEP.end)
        assert employed_span(SEP, date(2026, 10, 1), None) is None


class TestDiagnosis:
    def diag(self, record, actual, **kw):
        kw.setdefault("doj", OLD_DOJ)
        kw.setdefault("lwd", None)
        kw.setdefault("basis", CAL)
        return diagnose_paid_days(record, D(actual), **kw)

    def test_match(self):
        d = self.diag(rec(), 30)
        assert d.status is ComparisonStatus.MATCH and d.causes == []

    @pytest.mark.parametrize(
        ("record_kw", "actual", "extra", "cause"),
        [
            ({"present_days": 23, "absent_days": 3, "lop_days": 3}, 30, {}, "LOP of 3 days not deducted"),
            ({"present_days": 23, "absent_days": 3, "lop_days": 3}, 24, {}, "deducted twice"),
            ({"present_days": 25, "absent_days": 1, "lop_days": 1, "lop_reversal_days": 1}, 30, {}, "LOP reversal of 1 days added"),
            ({"working_days": 14, "week_offs": 2, "present_days": 14}, 30, {"doj": date(2026, 9, 15)}, "14 days before DOJ / after LWD"),
            ({}, 26, {}, "Week offs treated as unpaid"),
        ],
    )
    def test_known_causes(self, record_kw, actual, extra, cause):
        d = self.diag(rec(**record_kw), actual, **extra)
        assert d.status is ComparisonStatus.MISMATCH
        assert any(cause in c for c in d.causes), d.causes

    def test_basis_mismatch_in_february(self):
        feb = rec("2026-02", working_days=24, week_offs=4, present_days=24)
        d = self.diag(feb, 30)
        assert d.expected == D(28) and d.difference == D(2)
        assert any("matches FIXED_30 basis" in c for c in d.causes)

    def test_unknown_cause(self):
        d = self.diag(rec(), "27.5")
        assert "No single known cause" in d.causes[0]
        assert d.expected_trace.value == D(30)
