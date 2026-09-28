"""TDS for one employee and period."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.calculations.pt import PTResult
from payroll_copilot.calculations.tds import Declaration, TDSInput, TDSResult, calculate_tds
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import RuleNotFoundError
from payroll_copilot.core.money import RUPEE, format_inr, round_money
from payroll_copilot.db.models import Employee, TDSDeclaration, TDSOverride
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.repositories.payroll_repo import PayrollRepository
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.projection import (
    ESTIMATED,
    NOT_EMPLOYED,
    PROJECTED,
    WageProjector,
)
from payroll_copilot.services.pt_service import PTService
from payroll_copilot.services.salary_service import SalaryCalculationView, SalaryService

ZERO = Decimal(0)


def render_tds(result: TDSResult, employee_code: str, period: str) -> str:
    label = PayrollPeriod.parse(period).label
    if not result.applicable:
        return f"TDS | {employee_code} | {label}: not applicable ({result.reason})"
    if not result.configured:
        return f"TDS | {employee_code} | {label}: NOT CALCULATED ({result.reason})"
    a = result.annual
    lines = [
        f"TDS | {employee_code} | {label} | rule {result.rule.label} [{result.rule.status.value}]",
        f"  Gross taxable salary (FY)  {format_inr(result.gross_taxable_salary):>14}",
        f"  Standard deduction         {format_inr(-result.standard_deduction):>14}",
    ]
    if result.professional_tax:
        lines.append(f"  Professional Tax           {format_inr(-result.professional_tax):>14}")
    for code, amount in result.section_deductions.items():
        lines.append(f"  Deduction {code:<16} {format_inr(-amount):>14}")
    lines += [
        f"  Taxable income             {format_inr(a.taxable_income):>14}",
        f"  Tax on slabs               {format_inr(a.slab_tax):>14}",
    ]
    if a.rebate:
        lines.append(f"  Rebate                     {format_inr(-a.rebate):>14}")
    if a.surcharge:
        lines.append(f"  Surcharge                  {format_inr(a.surcharge):>14}")
    lines += [
        f"  Cess                       {format_inr(a.cess):>14}",
        f"  Annual tax                 {format_inr(a.total_tax):>14}",
        f"  TDS already deducted       {format_inr(result.tds_already_deducted):>14}",
        f"  Remaining months           {result.remaining_months:>14}",
        f"  Monthly TDS                {format_inr(result.monthly_tds):>14}"
        + (" (OVERRIDDEN)" if result.overridden else ""),
    ]
    lines.extend(f"  WARNING: {w}" for w in result.warnings)
    return "\n".join(lines)


class TDSService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.salary_service = SalaryService(session)
        self.projector = WageProjector(self.salary_service)
        self.rules = RuleService(session)
        self.payroll = PayrollRepository(session)

    def _declarations(self, employee: Employee, fy: str) -> list[Declaration]:
        rows = self.session.scalars(
            select(TDSDeclaration).where(
                TDSDeclaration.employee_id == employee.id,
                TDSDeclaration.financial_year == fy,
                TDSDeclaration.regime == employee.tax_regime,
            )
        )
        return [
            Declaration(
                section_code=r.section_code,
                amount=r.proof_amount if r.proof_amount is not None else r.declared_amount,
            )
            for r in rows
        ]

    def _pt_for_tax(
        self, employee: Employee, period: PayrollPeriod, pt: PTResult | None,
        employed: list[PayrollPeriod], warnings: list[str],
    ) -> Decimal:  # fmt: skip
        if pt is None or not pt.applicable or not pt.configured or not pt.levied:
            return ZERO
        if not pt.deduction_month:
            warnings.append(
                "PT for income-tax deduction not projected (no PT deducted this month); "
                "recheck in a PT deduction month"
            )
            return ZERO
        context = build_employee_context(employee, period)
        rule = self.rules.resolve(RuleType.PT, period, context).rule
        count = sum(1 for m in employed if m.month in rule.deduction_months)
        warnings.append(
            f"PT for tax projected as {pt.amount} × {count} deduction months "
            "(month overrides such as February not applied)"
        )
        return pt.amount * count

    def calculate_for_salary(
        self,
        employee: Employee,
        period: PayrollPeriod,
        salary: SalaryCalculationView,
        pt: PTResult | None = None,
        override_amount: Decimal | None = None,
        override_reason: str | None = None,
    ) -> TDSResult:
        if not salary.salary.lines:
            return TDSResult.not_applicable(f"no earnings in {period}")
        if override_amount is None:  # an approved override applies unless a what-if is given
            stored = self.session.scalar(
                select(TDSOverride).where(
                    TDSOverride.employee_id == employee.id,
                    TDSOverride.period == str(period),
                    TDSOverride.active.is_(True),
                ).order_by(TDSOverride.id.desc())
            )
            if stored is not None:
                override_amount = stored.amount
                override_reason = f"approved override #{stored.approval_id}: {stored.reason}"
        context = build_employee_context(employee, period)
        try:
            resolved = self.rules.resolve(RuleType.TDS, period, context)
        except RuleNotFoundError as exc:
            return TDSResult.not_configured(str(exc))
        rule, warnings = resolved.rule, list(resolved.warnings)
        fy = period.financial_year

        heads = self.salary_service.salary.heads()
        current = compute_wage_base("TAXABLE_EARNINGS", salary.salary.earnings, heads).value
        rows = self.projector.months(
            employee, fy.periods(), period, current, "TAXABLE_EARNINGS"
        )
        employed = {k: v for k, v in rows.items() if v[2] != NOT_EMPLOYED}
        monthly_taxable = {k: amount for k, (_, amount, _) in employed.items()}
        employed_months = [m for m, _, _ in employed.values()]
        remaining = sum(1 for m in employed_months if m >= period)
        past = [m for m in employed_months if m < period]
        if any(tag in (ESTIMATED, PROJECTED) for _, _, tag in employed.values()):
            warnings.append(
                "FY salary includes structure-based estimates/projections for months "
                "without payroll data"
            )
        if fy.start < employee.doj <= fy.end:
            warnings.append(
                f"Joined on {employee.doj} during FY {fy.label}: previous employer income/TDS "
                "(Form 12B) not provided, assumed nil"
            )
        if employee.lwd and period.contains(employee.lwd):
            warnings.append("Final month (LWD in period): balance tax for the FY recovered now")

        base_input = dict(
            employee_code=employee.employee_code,
            period=str(period),
            monthly_taxable=monthly_taxable,
            professional_tax_annual=(
                self._pt_for_tax(employee, period, pt, employed_months, warnings)
                if rule.allow_professional_tax_deduction
                else ZERO
            ),
            declarations=self._declarations(employee, fy.label),
            remaining_months=remaining,
        )  # fmt: skip

        history = self.payroll.deduction_history(employee.id, "TDS", past)
        if history:
            already = sum(history.values(), ZERO)
            note = f"actual TDS from payroll history for {sorted(history)}"
            missing = [str(m) for m in past if str(m) not in history]
            if missing:
                warnings.append(f"No actual payroll TDS for {missing}; counted as ₹0")
        elif past:
            first = calculate_tds(TDSInput(**base_input), rule)
            annual_tax = first.annual.total_tax
            already = round_money(annual_tax * len(past) / len(employed_months), rule.rounding_mode,
                                  RUPEE)  # fmt: skip
            note = (
                f"no payroll history: assumed even deduction for {len(past)} earlier months "
                f"({annual_tax} × {len(past)}/{len(employed_months)})"
            )
            if annual_tax:
                warnings.append(f"TDS so far {note}")
        else:
            already, note = ZERO, "first month of employment in FY"

        return calculate_tds(
            TDSInput(
                **base_input,
                tds_already_deducted=already,
                tds_history_note=note,
                override_amount=override_amount,
                override_reason=override_reason,
            ),
            rule,
            warnings,
        )

    def calculate(
        self,
        employee_code: str,
        period: PayrollPeriod,
        override_amount: Decimal | None = None,
        override_reason: str | None = None,
    ) -> TDSResult:
        salary =self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        pt = PTService(self.session).calculate_for_salary(employee, period, salary)
        return self.calculate_for_salary(
            employee, period, salary, pt, override_amount, override_reason
        )
