from datetime import date, timedelta
from decimal import Decimal

import pytest

from payroll_copilot.calculations.attendance import compute_paid_days
from payroll_copilot.calculations.pay_summary import summarize_pay
from payroll_copilot.calculations.salary import (
    LopReversalInput,
    SalarySegmentInput,
    calculate_earnings,
)
from payroll_copilot.core.exceptions import StructureError
from payroll_copilot.domain.attendance import AttendanceRecord
from payroll_copilot.domain.enums import LopAllocation, PaidDaysBasis
from payroll_copilot.domain.salary import PayLine

D = Decimal
CAL, F30, WD = PaidDaysBasis.CALENDAR, PaidDaysBasis.FIXED_30, PaidDaysBasis.WORKING_DAYS
OLD_DOJ = date(2020, 1, 1)

STD_60K = {"BASIC": 30000, "HRA": 15000, "CONVEYANCE": 1600, "MEDICAL": 1250, "SPECIAL": 12150}
STD_70K = {"BASIC": 35000, "HRA": 17500, "CONVEYANCE": 1600, "MEDICAL": 1250, "SPECIAL": 14650}
STD_35K = {"BASIC": 17500, "HRA": 8750, "CONVEYANCE": 1600, "MEDICAL": 1250, "SPECIAL": 5900}


def comps(amounts) -> list[PayLine]:
    return [PayLine(head_code=h, amount=D(a)) for h, a in amounts.items()]


def seg(amounts, start, end, label="S") -> SalarySegmentInput:
    return SalarySegmentInput(
        label=label, active_from=start, active_to=end,
        monthly_gross=D(sum(amounts.values())), components=comps(amounts),
    )  # fmt: skip


def sundays(start: date, end: date) -> int:
    return sum(1 for n in range((end - start).days + 1) if (start + timedelta(n)).weekday() == 6)


def paid_days(period="2026-09", doj=OLD_DOJ, lwd=None, basis=CAL, lop=0, reversal=0):
    y, m = map(int, period.split("-"))
    start = max(date(y, m, 1), doj)
    last = (date(y + m // 12, m % 12 + 1, 1) - timedelta(1))
    end = min(last, lwd) if lwd else last
    employed = (end - start).days + 1
    wo = sundays(start, end)
    record = AttendanceRecord(
        period=period, calendar_days=last.day, working_days=D(employed - wo), week_offs=D(wo),
        holidays=D(0), present_days=D(employed - wo - lop), absent_days=D(lop), lop_days=D(lop),
        lop_reversal_days=D(reversal),
    )  # fmt: skip
    return compute_paid_days(record, doj=doj, lwd=lwd, basis=basis)


def calc(segments, pd, heads, basis=CAL, **kw):
    return calculate_earnings(segments, pd, heads, basis=basis, **kw)


def assert_reduction_identity(r):
    red = r.reduction
    rebuilt = (red["full_month_gross"] - red["reduction_lop"] - red["reduction_not_employed"]
               - red["reduction_mid_month_revision"] + red["rounding_difference"])  # fmt: skip
    assert abs(rebuilt - red["earned_gross"]) <= D("0.02")


SEP1, SEP15, SEP16, SEP30 = date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 30)


class TestSingleSegment:
    def test_full_month_pays_full_gross(self, heads):
        r = calc([seg(STD_60K, SEP1, SEP30)], paid_days(), heads)
        assert r.gross_earnings == D(60000)
        assert r.reduction["reduction_lop"] == 0 and r.reduction["rounding_difference"] == 0

    def test_lop(self, heads):
        r = calc([seg(STD_60K, SEP1, SEP30)], paid_days(lop=3), heads)
        assert r.gross_earnings == D(54000)
        assert r.reduction["reduction_lop"] == D(6000)
        basic = next(line for line in r.lines if line.head_code == "BASIC")
        assert (basic.amount, basic.formula) == (D(27000), "30000 × 27 / 30")

    def test_joiner_rounds_per_line(self, heads):
        doj = date(2026, 9, 15)
        r = calc([seg(STD_35K, doj, SEP30)], paid_days(doj=doj), heads)
        amounts = {line.head_code: line.amount for line in r.lines}
        assert amounts == {"BASIC": D(9333), "HRA": D(4667), "SPECIAL": D(3147),
                           "CONVEYANCE": D(853), "MEDICAL": D(667)}  # fmt: skip
        assert r.gross_earnings == D(18667)
        assert r.reduction["reduction_not_employed"] == D("16333.33")
        assert_reduction_identity(r)

    def test_non_prorated_head_paid_in_full(self, heads):
        heads = {**heads, "MEDICAL": heads["MEDICAL"].model_copy(update={"is_prorated": False})}
        r = calc([seg(STD_60K, SEP1, SEP30)], paid_days(lop=15), heads)
        medical = next(line for line in r.lines if line.head_code == "MEDICAL")
        assert medical.amount == D(1250) and medical.paid_days is None

    def test_lines_in_head_display_order(self, heads):
        r = calc([seg(STD_60K, SEP1, SEP30)], paid_days(), heads)
        assert [line.head_code for line in r.lines] == ["BASIC", "HRA", "SPECIAL", "CONVEYANCE", "MEDICAL"]

    def test_not_employed_gives_zero(self, heads):
        r = calc([], paid_days(doj=date(2026, 10, 1)), heads)
        assert r.gross_earnings == 0 and r.lines == []

    @pytest.mark.parametrize("day", [1, 2, 10, 15, 29, 30])
    def test_gross_close_to_exact_proration_for_any_doj(self, heads, day):
        doj = date(2026, 9, day)
        pd = paid_days(doj=doj, lop=0)
        r = calc([seg(STD_60K, doj, SEP30)], pd, heads)
        exact = D(60000) * pd.value / 30
        assert r.gross_earnings <= D(60000)
        assert abs(r.gross_earnings - exact) <= D("2.5")  # at most 0.5 per line
        assert_reduction_identity(r)


class TestMidMonthRevision:
    def two_segments(self):
        return [seg(STD_60K, SEP1, SEP15, "old"), seg(STD_70K, SEP16, SEP30, "new")]

    def test_revision_without_lop(self, heads):
        r = calc(self.two_segments(), paid_days(), heads)
        basic = [line.amount for line in r.lines if line.head_code == "BASIC"]
        assert basic == [D(15000), D(17500)]
        assert r.gross_earnings == D(65000)
        assert r.reduction["full_month_gross"] == D(70000)
        assert r.reduction["reduction_mid_month_revision"] == D(5000)
        assert r.earnings[0] == PayLine(head_code="BASIC", amount=D(32500))  # merged per head
        assert_reduction_identity(r)

    def test_lop_last_segment(self, heads):
        r = calc(self.two_segments(), paid_days(lop=2), heads)
        assert [s.lop_days for s in r.segments] == [D(0), D(2)]
        assert any("LAST_SEGMENT" in w for w in r.warnings)
        assert_reduction_identity(r)

    def test_lop_proportional(self, heads):
        r = calc(self.two_segments(), paid_days(lop=2), heads,
                 lop_allocation=LopAllocation.PROPORTIONAL)  # fmt: skip
        assert [s.lop_days for s in r.segments] == [D(1), D(1)]

    def test_lop_overflows_into_earlier_segment(self, heads):
        segments = [seg(STD_60K, SEP1, date(2026, 9, 25), "old"),
                    seg(STD_70K, date(2026, 9, 26), SEP30, "new")]  # fmt: skip
        r = calc(segments, paid_days(lop=7), heads)
        assert [s.lop_days for s in r.segments] == [D(2), D(5)]
        assert r.segments[1].paid_days == 0

    def test_fixed_30_split_in_31_day_month(self, heads):
        jul = [seg(STD_60K, date(2026, 7, 1), date(2026, 7, 15), "old"),
               seg(STD_70K, date(2026, 7, 16), date(2026, 7, 31), "new")]  # fmt: skip
        r = calc(jul, paid_days("2026-07", basis=F30), heads, basis=F30)
        assert [s.payable_days for s in r.segments] == [D("14.52"), D("15.48")]
        assert sum(s.paid_days for s in r.segments) == D(30)

    def test_gap_in_structures(self, heads):
        with pytest.raises(StructureError, match="cover 25 of 30"):
            calc([seg(STD_60K, SEP1, SEP15, "a"), seg(STD_70K, date(2026, 9, 21), SEP30, "b")],
                 paid_days(), heads)  # fmt: skip

    def test_overlapping_segments(self, heads):
        with pytest.raises(StructureError, match="overlap"):
            calc([seg(STD_60K, SEP1, SEP16, "a"), seg(STD_70K, SEP16, SEP30, "b")],
                 paid_days(), heads)  # fmt: skip

    def test_working_days_basis_needs_calendar(self, heads):
        with pytest.raises(StructureError, match="work calendar"):
            calc(self.two_segments(), paid_days(basis=WD), heads, basis=WD)


class TestExtras:
    def test_lop_reversal_is_arrear_at_previous_rate(self, heads):
        reversal = LopReversalInput(period="2026-08", days=D(1), basis_days=D(31),
                                    components=comps(STD_60K))  # fmt: skip
        r = calc([seg(STD_60K, SEP1, SEP30)], paid_days(lop=1, reversal=1), heads,
                 lop_reversal=reversal)  # fmt: skip
        arrears = [line for line in r.lines if line.is_arrear]
        assert {line.head_code: line.amount for line in arrears}["BASIC"] == D(968)  # 30000/31
        assert r.reduction["lop_reversal"] == sum(line.amount for line in arrears)
        assert PayLine(head_code="BASIC", amount=D(968), is_arrear=True) in r.earnings
        summary = summarize_pay(r.earnings, [], [], heads)
        assert summary.gross_earnings == r.gross_earnings
        assert summary.wage_bases["PF_WAGE"] == D(29000) + D(968)

    def test_variable_earnings(self, heads):
        r = calc([seg(STD_60K, SEP1, SEP30)], paid_days(lop=3), heads,
                 variable_earnings=[PayLine(head_code="BONUS", amount=D(5000))])  # fmt: skip
        assert r.gross_earnings == D(59000)
        assert r.reduction["variable_earnings"] == D(5000)
        assert r.reduction["earned_gross"] == D(54000)

    def test_structure_must_contain_only_earnings(self, heads):
        with pytest.raises(StructureError, match="not an earning"):
            calc([seg({"BASIC": 1000, "EE_PF": 120}, SEP1, SEP30)], paid_days(), heads)

    def test_needs_paid_days_result(self, heads):
        r = calc([seg(STD_60K, SEP1, SEP30)], paid_days(), heads)
        with pytest.raises(StructureError, match="compute_paid_days"):
            calc([seg(STD_60K, SEP1, SEP30)], r.trace, heads)

    def test_missing_structure(self, heads):
        with pytest.raises(StructureError, match="No salary structure"):
            calc([], paid_days(), heads)

    def test_trace_text(self, heads):
        text = calc([seg(STD_60K, SEP1, SEP30)], paid_days(lop=2), heads).trace.to_text()
        assert "payable 30 − LOP 2 = 28 days" in text
        assert "Basic [S]: 30000 × 28 / 30 = ₹28,000" in text
        assert "Result: ₹56,000" in text
