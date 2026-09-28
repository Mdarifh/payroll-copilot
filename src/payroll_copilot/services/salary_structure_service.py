"""Salary structure view for one employee and payroll period.

Shows the full-month structure (earnings), which statutory deductions and
employer contributions apply to the employee and why, and whether the stored
component amounts match the structure template. Amounts of statutory heads are
computed by their own engines (PF, ESIC, PT, LWF, TDS) in later phases.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pay_summary import summarize_pay
from payroll_copilot.calculations.structure import StructureCheck, check_assignment_against_template
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import ComparisonStatus, HeadCategory, HeadType
from payroll_copilot.domain.salary import BucketLine, SalaryHeadInfo
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.repositories.salary_repo import SalaryRepository

# Statutory head -> employee flags that must all be true for the head to apply.
# Final eligibility (ceilings, thresholds, age limits, state) is decided by the
# rule engines; this only reflects the employee's configuration.
STATUTORY_HEAD_FLAGS: dict[str, tuple[str, ...]] = {
    "EE_PF": ("pf_applicable",),
    "ER_EPF": ("pf_applicable",),
    "EPS": ("pf_applicable", "eps_eligible"),
    "EDLI": ("pf_applicable",),
    "EPF_ADMIN": ("pf_applicable",),
    "EE_ESIC": ("esic_applicable",),
    "ER_ESIC": ("esic_applicable",),
    "PT": ("pt_applicable",),
    "LWF_EE": ("lwf_applicable",),
    "LWF_ER": ("lwf_applicable",),
    "TDS": (),
}

_ENGINE_NOTE = {
    "EE_PF": "PF engine", "ER_EPF": "PF engine", "EPS": "PF engine", "EDLI": "PF engine",
    "EPF_ADMIN": "PF engine", "EE_ESIC": "ESIC engine", "ER_ESIC": "ESIC engine",
    "PT": "PT engine ({state} rules)", "LWF_EE": "LWF engine ({state} rules)",
    "LWF_ER": "LWF engine ({state} rules)", "TDS": "TDS engine (projected taxable income)",
}  # fmt: skip


class ComplianceHead(BaseModel):
    model_config = ConfigDict(frozen=True)

    head_code: str
    name: str
    category: HeadCategory
    applicable: bool
    reason: str


class StructureSegment(BaseModel):
    """One salary assignment active during (part of) the period."""

    model_config = ConfigDict(frozen=True)

    structure: str
    reason: str
    effective_from: date
    effective_to: date | None
    active_from: date  # clipped to the period (and DOJ/LWD)
    active_to: date
    monthly_gross: Decimal
    earnings: list[BucketLine]
    wage_bases: dict[str, Decimal]
    template_check: StructureCheck


class SalaryStructureView(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    employee_name: str
    period: str
    work_state: str
    segments: list[StructureSegment]
    compliance: list[ComplianceHead]
    warnings: list[str] = Field(default_factory=list)

    def to_text(self) -> str:
        lines = [
            f"Salary structure | {self.employee_code} ({self.employee_name}) | "
            f"{PayrollPeriod.parse(self.period).label} | state {self.work_state}"
        ]
        for seg in self.segments:
            lines.append("")
            lines.append(
                f"{seg.structure} ({seg.reason}) effective {seg.effective_from} to "
                f"{seg.effective_to or 'open'}; active in period {seg.active_from} to "
                f"{seg.active_to}"
            )
            lines.append("  EARNINGS (full month)")
            for e in seg.earnings:
                lines.append(f"    {e.name:<24} {format_inr(e.amount):>12}")
            lines.append(f"    {'Monthly gross':<24} {format_inr(seg.monthly_gross):>12}")
            lines.append("  WAGE BASES")
            for base, amount in seg.wage_bases.items():
                lines.append(f"    {base:<24} {format_inr(amount):>12}")
            check = seg.template_check
            lines.append(
                "  TEMPLATE CHECK: "
                + ("OK - components match template" if check.is_consistent else "MISMATCH")
            )
            for c in check.lines:
                if c.status is not ComparisonStatus.MATCH:
                    lines.append(
                        f"    {c.head_code}: expected {c.expected} actual {c.actual} "
                        f"diff {c.difference} [{c.status}]"
                    )
        for category, title in (
            (HeadCategory.DEDUCTION, "EMPLOYEE DEDUCTIONS (statutory)"),
            (HeadCategory.EMPLOYER_CONTRIBUTION, "EMPLOYER CONTRIBUTIONS (compliance)"),
        ):
            lines.append("")
            lines.append(title)
            for head in (h for h in self.compliance if h.category is category):
                mark = "YES" if head.applicable else "NO "
                lines.append(f"  [{mark}] {head.name:<22} {head.reason}")
        lines.extend(f"WARNING: {w}" for w in self.warnings)
        return "\n".join(lines)


def _compliance_heads(
    employee: Employee, heads: dict[str, SalaryHeadInfo]
) -> list[ComplianceHead]:
    result = []
    statutory = [
        h for h in heads.values()
        if h.head_type is HeadType.STATUTORY and h.category is not HeadCategory.EARNING
    ]  # fmt: skip
    for head in statutory:
        flags = STATUTORY_HEAD_FLAGS.get(head.code)
        if flags is None:
            result.append(
                ComplianceHead(
                    head_code=head.code, name=head.name, category=head.category,
                    applicable=False, reason="No applicability mapping configured",
                )  # fmt: skip
            )
            continue
        failed = [f for f in flags if not getattr(employee, f)]
        engine = _ENGINE_NOTE[head.code].format(state=employee.work_state)
        reason = (
            f"computed by {engine}"
            if not failed
            else "not applicable: " + ", ".join(f"{f} = false" for f in failed)
        )
        result.append(
            ComplianceHead(
                head_code=head.code, name=head.name, category=head.category,
                applicable=not failed, reason=reason,
            )  # fmt: skip
        )
    return result


class SalaryStructureService:
    def __init__(self, session: Session) -> None:
        self.employees = EmployeeRepository(session)
        self.salary = SalaryRepository(session)

    def get_salary_structure(
        self, employee_code: str, period: PayrollPeriod
    ) -> SalaryStructureView:
        employee = self.employees.get_by_code(employee_code)
        heads = self.salary.heads()
        rounding = get_settings().default_rounding_mode

        start = max(period.start, employee.doj)
        end = min(period.end, employee.lwd) if employee.lwd else period.end
        warnings: list[str] = []
        segments: list[StructureSegment] = []
        if start > end:
            warnings.append(
                f"{employee.employee_code} was not employed in {period} "
                f"(DOJ {employee.doj}, LWD {employee.lwd})"
            )
            assignments = []
        else:
            assignments = self.salary.assignments_between(employee.id, start, end)
            if not assignments:
                warnings.append(f"No salary structure effective between {start} and {end}")

        for a in assignments:
            summary = summarize_pay(a.components, [], [], heads)
            active_from = max(start, a.effective_from)
            active_to = min(end, a.effective_to) if a.effective_to else end
            segments.append(
                StructureSegment(
                    structure=f"{a.structure.code} v{a.structure.version}",
                    reason=a.reason.value,
                    effective_from=a.effective_from,
                    effective_to=a.effective_to,
                    active_from=active_from,
                    active_to=active_to,
                    monthly_gross=summary.gross_earnings,
                    earnings=summary.earnings,
                    wage_bases=summary.wage_bases,
                    template_check=check_assignment_against_template(
                        a, heads, rounding_mode=rounding, employee_code=employee.employee_code
                    ),
                )
            )
            if summary.gross_earnings != a.monthly_gross:
                warnings.append(
                    f"{a.structure.code}: components total {summary.gross_earnings} but "
                    f"monthly_gross is {a.monthly_gross}"
                )

        if len(segments) > 1:
            warnings.append(
                f"Salary changed within {period} (on {segments[1].active_from}); "
                "the salary engine prorates each segment separately"
            )
        if segments and segments[0].active_from > period.start:
            warnings.append(f"Employee active only from {segments[0].active_from} (mid-month)")
        if segments and segments[-1].active_to < period.end:
            warnings.append(f"Employee active only until {segments[-1].active_to} (mid-month)")

        return SalaryStructureView(
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            period=str(period),
            work_state=employee.work_state,
            segments=segments,
            compliance=_compliance_heads(employee, heads),
            warnings=warnings,
        )
