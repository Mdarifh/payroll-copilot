"""Attendance, LOP and paid-days engine (deterministic).

Paid days = payable days in the employed span − LOP days, where payable days
depend on the paid-days basis:

* CALENDAR      basis = calendar days of the month; payable = employed calendar days
* FIXED_30/26   basis = 30 / 26; full month payable = basis. Partial month
                (DOJ/LWD inside the month) uses basis × employed / calendar days
                and warns, because conventions differ between companies.
* WORKING_DAYS  basis = working days of the month; payable = employed working days

LOP reversal days (LOP wrongly deducted in an earlier month) are NOT added to
this month's paid days; they are reported separately so they can be paid as an
arrear at the earlier month's rate.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from payroll_copilot.calculations.trace import CalculationResult, TraceStep
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import AttendanceError
from payroll_copilot.core.money import PAISA, RoundingMode, round_money
from payroll_copilot.domain.attendance import AttendanceRecord
from payroll_copilot.domain.enums import ComparisonStatus, PaidDaysBasis

ZERO = Decimal(0)
_FIXED_BASIS = {PaidDaysBasis.FIXED_30: Decimal(30), PaidDaysBasis.FIXED_26: Decimal(26)}
_COUNT_FIELDS = (
    "working_days", "week_offs", "holidays", "present_days", "paid_leave_days",
    "absent_days", "lop_days", "lop_reversal_days",
)  # fmt: skip


def employed_span(period: PayrollPeriod, doj: date, lwd: date | None) -> tuple[date, date] | None:
    """First and last employed day inside the period, or None if not employed."""
    start = max(period.start, doj)
    end = min(period.end, lwd) if lwd else period.end
    return (start, end) if start <= end else None


def validate_attendance(
    record: AttendanceRecord, period: PayrollPeriod, doj: date, lwd: date | None
) -> list[str]:
    """Raise on impossible data; return warnings for inconsistent but usable data."""
    if record.period != str(period):
        raise AttendanceError(f"Attendance is for {record.period}, not {period}")
    if record.calendar_days != period.calendar_days:
        raise AttendanceError(
            f"calendar_days {record.calendar_days} but {period.label} has {period.calendar_days}"
        )
    for name in _COUNT_FIELDS:
        if getattr(record, name) < 0:
            raise AttendanceError(f"{name} cannot be negative ({getattr(record, name)})")

    warnings: list[str] = []
    span = employed_span(period, doj, lwd)
    employed = Decimal((span[1] - span[0]).days + 1) if span else ZERO
    accounted = record.working_days + record.week_offs + record.holidays
    if accounted != employed:
        warnings.append(
            f"working days + week offs + holidays = {accounted}, but employee was employed "
            f"{employed} days in {period}"
        )
    marked = record.present_days + record.paid_leave_days + record.absent_days
    if marked != record.working_days:
        warnings.append(
            f"present + paid leave + absent = {marked}, but working days = {record.working_days}"
        )
    if record.lop_days > record.absent_days:
        warnings.append(
            f"LOP days {record.lop_days} exceed absent days {record.absent_days} "
            "(LOP from week offs/holidays sandwich rules or unpaid leave?)"
        )
    return warnings


def compute_paid_days(
    record: AttendanceRecord,
    *,
    doj: date,
    lwd: date | None,
    basis: PaidDaysBasis,
    month_working_days: Decimal | None = None,
    employee_code: str | None = None,
) -> CalculationResult:
    period = PayrollPeriod.parse(record.period)
    warnings = validate_attendance(record, period, doj, lwd)
    calendar_days = Decimal(period.calendar_days)
    span = employed_span(period, doj, lwd)
    steps: list[TraceStep] = [
        TraceStep(label="Calendar days", formula=f"days in {period.label}", result=calendar_days)
    ]

    if span is None:
        warnings.append(f"Not employed in {period} (DOJ {doj}, LWD {lwd}); paid days = 0")
        employed = pre_doj = post_lwd = ZERO
    else:
        employed = Decimal((span[1] - span[0]).days + 1)
        pre_doj = Decimal((span[0] - period.start).days)
        post_lwd = Decimal((period.end - span[1]).days)
        steps.append(
            TraceStep(
                label="Employed days in month",
                formula=f"{span[0]} to {span[1]}",
                values={"days_before_doj": pre_doj, "days_after_lwd": post_lwd},
                result=employed,
            )
        )
    full_month = span is not None and pre_doj == 0 and post_lwd == 0

    # --- basis and payable days -------------------------------------------------
    if basis is PaidDaysBasis.CALENDAR:
        basis_days = calendar_days
        payable = employed
        payable_formula = "employed calendar days"
    elif basis in _FIXED_BASIS:
        basis_days = _FIXED_BASIS[basis]
        if full_month:
            payable = basis_days
            payable_formula = f"full month = {basis_days}"
        else:
            raw = basis_days * employed / calendar_days
            payable = round_money(raw, RoundingMode.HALF_UP, PAISA)
            payable_formula = f"{basis_days} × {employed} / {calendar_days}"
            if span is not None:
                warnings.append(
                    f"Partial month on {basis} basis: payable days prorated as "
                    f"{payable_formula}; confirm this matches company policy"
                )
    else:  # WORKING_DAYS
        if month_working_days is None:
            if not full_month and span is not None:
                raise AttendanceError(
                    "WORKING_DAYS basis for a partial month needs month_working_days "
                    "(company work calendar)"
                )
            month_working_days = record.working_days
        basis_days = Decimal(month_working_days)
        payable = record.working_days if span is not None else ZERO
        payable_formula = "employed working days"

    steps.append(TraceStep(label="Paid-days basis", formula=basis.value, result=basis_days))
    steps.append(
        TraceStep(label="Payable days before LOP", formula=payable_formula, result=payable)
    )

    # --- LOP ---------------------------------------------------------------------
    if record.lop_days > payable:
        raise AttendanceError(f"LOP days {record.lop_days} exceed payable days {payable}")
    paid = payable - record.lop_days
    steps.append(
        TraceStep(
            label="Paid days",
            formula=f"{payable} − LOP {record.lop_days}",
            values={"payable_days": payable, "lop_days": record.lop_days},
            result=paid,
        )
    )
    if record.lop_reversal_days:
        steps.append(
            TraceStep(
                label="LOP reversal days",
                result=record.lop_reversal_days,
                note="paid separately as arrear at the earlier month's rate; "
                "not added to this month's paid days",
            )
        )

    return CalculationResult(
        component="PAID_DAYS",
        unit="DAYS",
        employee_code=employee_code,
        period=str(period),
        inputs={
            **record.model_dump(exclude={"period"}),
            "doj": doj,
            "lwd": lwd,
            "basis": basis.value,
        },
        formula="Paid days = payable days in employed span − LOP days",
        steps=steps,
        raw_value=paid,
        value=paid,
        breakdown={
            "calendar_days": calendar_days,
            "basis_days": basis_days,
            "employed_days": employed,
            "days_before_doj": pre_doj,
            "days_after_lwd": post_lwd,
            "payable_days": payable,
            "lop_days": record.lop_days,
            "paid_days": paid,
            "lop_reversal_days": record.lop_reversal_days,
        },
        warnings=warnings,
    )


# ------------------------------------------------------------------ diagnosis


class PaidDaysDiagnosis(BaseModel):
    model_config = ConfigDict(frozen=True)

    expected: Decimal
    actual: Decimal
    difference: Decimal  # actual − expected
    status: ComparisonStatus
    causes: list[str]
    expected_trace: CalculationResult


def diagnose_paid_days(
    record: AttendanceRecord,
    actual_paid_days: Decimal,
    *,
    doj: date,
    lwd: date | None,
    basis: PaidDaysBasis,
    month_working_days: Decimal | None = None,
    employee_code: str | None = None,
) -> PaidDaysDiagnosis:
    """Explain why actual paid days differ from expected (deterministic candidates)."""
    expected = compute_paid_days(
        record, doj=doj, lwd=lwd, basis=basis,
        month_working_days=month_working_days, employee_code=employee_code,
    )  # fmt: skip
    b = expected.breakdown
    actual = Decimal(actual_paid_days)
    diff = actual - expected.value
    if diff == 0:
        return PaidDaysDiagnosis(
            expected=expected.value, actual=actual, difference=diff,
            status=ComparisonStatus.MATCH, causes=[], expected_trace=expected,
        )  # fmt: skip

    causes: list[str] = []

    def add(condition: bool, text: str) -> None:
        if condition:
            causes.append(text)

    lop, reversal = b["lop_days"], b["lop_reversal_days"]
    not_employed = b["days_before_doj"] + b["days_after_lwd"]
    add(lop > 0 and diff == lop, f"LOP of {lop} days not deducted in actual")
    add(lop > 0 and diff == -lop, f"LOP of {lop} days deducted twice in actual")
    add(
        reversal > 0 and diff == reversal,
        f"LOP reversal of {reversal} days added to this month's paid days "
        "instead of being paid as arrear",
    )
    add(
        not_employed > 0 and diff == not_employed,
        f"{not_employed} days before DOJ / after LWD not excluded in actual",
    )
    add(record.week_offs > 0 and diff == -record.week_offs, "Week offs treated as unpaid")
    add(record.holidays > 0 and diff == -record.holidays, "Holidays treated as unpaid")

    for other in PaidDaysBasis:
        if other is basis:
            continue
        try:
            alt = compute_paid_days(
                record, doj=doj, lwd=lwd, basis=other,
                month_working_days=month_working_days, employee_code=employee_code,
            )  # fmt: skip
        except AttendanceError:
            continue
        add(
            alt.value == actual,
            f"Actual matches {other.value} basis ({alt.value}); configured basis is {basis.value}",
        )

    if not causes:
        causes.append(
            f"No single known cause explains a difference of {diff} days; "
            "check the attendance source used by payroll"
        )
    return PaidDaysDiagnosis(
        expected=expected.value, actual=actual, difference=diff,
        status=ComparisonStatus.MISMATCH, causes=causes, expected_trace=expected,
    )  # fmt: skip
