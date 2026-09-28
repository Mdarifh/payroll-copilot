"""Arrears for an employee: as-paid salary for past months and arrears paid this month."""

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.arrears import (
    ArrearChange,
    ArrearMonthInput,
    ArrearMonthResult,
    ArrearResult,
    arrear_lines_for_month,
    pf_arrear_impact,
    summarize_arrears,
)
from payroll_copilot.calculations.attendance import compute_paid_days, employed_span
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError, NotFoundError, RuleNotFoundError
from payroll_copilot.db.models import Arrear, Employee
from payroll_copilot.domain.enums import ArrearStatus, PaidDaysBasis, RuleType
from payroll_copilot.domain.salary import SalaryHeadInfo
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.repositories.salary_repo import SalaryRepository
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService

_FIXED = {PaidDaysBasis.FIXED_30: Decimal(30), PaidDaysBasis.FIXED_26: Decimal(26)}


def _months(start: str, end: str) -> list[PayrollPeriod]:
    first, last = PayrollPeriod.parse(start), PayrollPeriod.parse(end)
    if last < first:
        raise CalculationError(f"Arrear range {start}..{end} is reversed")
    months = [first]
    while months[-1] < last:
        months.append(months[-1].next())
    return months


class ArrearService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.salary = SalaryRepository(session)
        self.attendance = AttendanceRepository(session)
        self.rules = RuleService(session)

    def _rows(self, employee_id: int) -> list[Arrear]:
        return list(
            self.session.scalars(
                select(Arrear).where(
                    Arrear.employee_id == employee_id, Arrear.status != ArrearStatus.CANCELLED
                )
            )
        )

    def as_paid_overrides(
        self, employee: Employee, month: PayrollPeriod
    ) -> dict[str, tuple[Decimal, str]]:
        """{head: (old amount, paid_in_period)} for revisions of ``month`` paid later as arrear."""
        overrides = {}
        for row in self._rows(employee.id):
            months = _months(row.from_period, row.to_period)
            if month in months and PayrollPeriod.parse(row.paid_in_period) > month:
                overrides[row.head_code] = (row.old_monthly_amount, row.paid_in_period)
        return overrides

    def _paid_days(
        self, employee: Employee, month: PayrollPeriod, basis: PaidDaysBasis,
        arrear_days: Decimal | None,
    ) -> tuple[Decimal, Decimal, str]:  # fmt: skip
        record = self.attendance.get(employee.id, month)
        if record is not None:
            paid = compute_paid_days(record, doj=employee.doj, lwd=employee.lwd, basis=basis)
            basis_days = paid.breakdown["basis_days"]
            if arrear_days is not None:
                return arrear_days, basis_days, "arrear_days"
            return paid.value, basis_days, "attendance"
        if arrear_days is None:
            raise NotFoundError(
                f"Arrear for {month} needs attendance or arrear_days ({employee.employee_code})"
            )
        if basis is PaidDaysBasis.CALENDAR:
            return arrear_days, Decimal(month.calendar_days), "arrear_days"
        if basis in _FIXED:
            return arrear_days, _FIXED[basis], "arrear_days"
        raise NotFoundError(f"WORKING_DAYS arrear for {month} needs attendance")

    def for_payment(
        self,
        employee: Employee,
        period: PayrollPeriod,
        heads: dict[str, SalaryHeadInfo],
        basis: PaidDaysBasis,
    ) -> ArrearResult | None:
        rows = [r for r in self._rows(employee.id) if r.paid_in_period == str(period)]
        if not rows:
            return None
        warnings: list[str] = []
        by_month: dict[PayrollPeriod, list[Arrear]] = defaultdict(list)
        for row in rows:
            months = _months(row.from_period, row.to_period)
            if row.arrear_days is not None and len(months) > 1:
                raise CalculationError(
                    f"arrear_days given for a multi-month arrear ({row.from_period}.."
                    f"{row.to_period}, {row.head_code}); split the upload per month"
                )
            if months[-1] >= period:
                raise CalculationError(
                    f"Arrear for {row.to_period} cannot be paid in {period} (not a past month)"
                )
            for month in months:
                by_month[month].append(row)

        rounding = get_settings().default_rounding_mode
        results: list[ArrearMonthResult] = []
        for month in sorted(by_month):
            month_rows = by_month[month]
            span = employed_span(month, employee.doj, employee.lwd)
            if span is None:
                raise CalculationError(f"Arrear for {month} but employee not employed then")
            assignments = self.salary.assignments_between(employee.id, *span)
            if len(assignments) != 1:
                raise CalculationError(
                    f"Arrear month {month} has {len(assignments)} salary structures; "
                    "mid-month revisions in arrear months are not supported yet"
                )
            new_components = assignments[0].components
            revised = {c.head_code: c.amount for c in new_components}
            changes = []
            for row in month_rows:
                if revised.get(row.head_code) != row.new_monthly_amount:
                    warnings.append(
                        f"{month} {row.head_code}: arrear new amount {row.new_monthly_amount} "
                        f"differs from salary structure {revised.get(row.head_code)}"
                    )
                changes.append(ArrearChange(
                    head_code=row.head_code, old_monthly_amount=row.old_monthly_amount,
                    new_monthly_amount=row.new_monthly_amount,
                ))  # fmt: skip
            paid, basis_days, source = self._paid_days(
                employee, month, basis, month_rows[0].arrear_days
            )
            month_input = ArrearMonthInput(
                period=str(month), paid_days=paid, basis_days=basis_days, days_source=source,
                changes=changes, new_components=new_components,
            )  # fmt: skip
            lines = arrear_lines_for_month(month_input, heads, rounding)

            pf_rule_label, pf_old, pf_new, pf_impact, pf_note = None, {}, {}, {}, None
            if employee.pf_applicable:
                context = build_employee_context(employee, month)
                try:
                    rule = self.rules.resolve(RuleType.PF, month, context).rule
                    pf_rule_label = f"{rule.rule_code} v{rule.version}"
                    pf_old, pf_new, pf_impact = pf_arrear_impact(
                        month_input, heads, rule,
                        dict(employee_code=employee.employee_code,
                             wage_restricted=employee.pf_wage_restricted,
                             eps_eligible=employee.eps_eligible, dob=employee.dob),
                    )  # fmt: skip
                    if not any(pf_impact.values()):
                        pf_note = "no PF change: PF wage already at the ceiling of that month"
                except RuleNotFoundError as exc:
                    warnings.append(f"PF on arrear {month} not computed: {exc}")
            results.append(ArrearMonthResult(
                period=str(month), paid_days=paid, basis_days=basis_days, lines=lines,
                pf_rule=pf_rule_label, pf_old=pf_old, pf_new=pf_new, pf_impact=pf_impact,
                pf_note=pf_note,
            ))  # fmt: skip

        uploaded: dict[str, Decimal] = {}
        for row in rows:
            if row.amount is not None:
                uploaded[row.head_code] = uploaded.get(row.head_code, Decimal(0)) + row.amount
        return summarize_arrears(employee.employee_code, str(period), results, uploaded, warnings)
