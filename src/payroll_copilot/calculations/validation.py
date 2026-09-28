"""Expected vs actual comparison.

Every compared value becomes a ``ComparisonResult`` with expected, actual,
difference (actual − expected), status, and - filled in by root-cause analysis -
an issue type and human-readable candidate causes, plus the expected trace.
"""

from collections import Counter
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult
from payroll_copilot.core.money import format_inr
from payroll_copilot.domain.enums import ComparisonStatus

ZERO = Decimal(0)


class ComponentGroup(StrEnum):
    ATTENDANCE = "ATTENDANCE"
    EARNINGS = "EARNINGS"
    ARREAR = "ARREAR"
    PF = "PF"
    ESIC = "ESIC"
    PT = "PT"
    LWF = "LWF"
    TDS = "TDS"
    OTHER_DEDUCTION = "OTHER_DEDUCTION"
    TOTALS = "TOTALS"
    DATA_QUALITY = "DATA_QUALITY"


class IssueType(StrEnum):
    RULE_VERSION = "RULE_VERSION"
    CEILING = "CEILING"
    ROUNDING = "ROUNDING"
    LOP = "LOP"
    PRORATION = "PRORATION"
    ARREAR = "ARREAR"
    SLAB = "SLAB"
    ELIGIBILITY = "ELIGIBILITY"
    UPSTREAM = "UPSTREAM"  # consequence of another mismatch (e.g. wrong gross)
    DATA_QUALITY = "DATA_QUALITY"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class ComparisonResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    period: str
    component: str
    group: ComponentGroup
    unit: Literal["INR", "DAYS"] = "INR"
    expected: Decimal | None
    actual: Decimal | None
    difference: Decimal | None
    tolerance: Decimal = ZERO
    status: ComparisonStatus
    issue_type: IssueType | None = None
    root_causes: list[str] = Field(default_factory=list)
    expected_trace: CalculationResult | None = None

    @property
    def is_issue(self) -> bool:
        return self.status in (
            ComparisonStatus.MISMATCH,
            ComparisonStatus.MISSING_ACTUAL,
            ComparisonStatus.MISSING_EXPECTED,
        )

    def fmt(self, value: Decimal | None) -> str:
        if value is None:
            return "-"
        return f"{value.normalize():f} days" if self.unit == "DAYS" else format_inr(value)

    def with_diagnosis(self, issue_type: IssueType, causes: list[str]) -> "ComparisonResult":
        return self.model_copy(update={"issue_type": issue_type, "root_causes": causes})


def compare(
    employee_code: str,
    period: str,
    component: str,
    group: ComponentGroup,
    expected: Decimal | None,
    actual: Decimal | None,
    *,
    tolerance: Decimal = ZERO,
    unit: Literal["INR", "DAYS"] = "INR",
    trace: CalculationResult | None = None,
) -> ComparisonResult:
    if unit == "DAYS":
        tolerance = ZERO  # days must match exactly
    if actual is None and expected is None:
        status, diff = ComparisonStatus.MATCH, None
    elif actual is None:
        status = ComparisonStatus.MATCH if expected == 0 else ComparisonStatus.MISSING_ACTUAL
        diff = None if expected else ZERO
    elif expected is None:
        status = ComparisonStatus.MATCH if actual == 0 else ComparisonStatus.MISSING_EXPECTED
        diff = None if actual else ZERO
    else:
        diff = actual - expected
        if diff == 0:
            status = ComparisonStatus.MATCH
        elif abs(diff) <= tolerance:
            status = ComparisonStatus.WITHIN_TOLERANCE
        else:
            status = ComparisonStatus.MISMATCH
    return ComparisonResult(
        employee_code=employee_code, period=period, component=component, group=group,
        unit=unit, expected=expected, actual=actual, difference=diff, tolerance=tolerance,
        status=status, expected_trace=trace,
    )  # fmt: skip


class EmployeeValidation(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    employee_name: str
    period: str
    results: list[ComparisonResult]
    note: str | None = None  # e.g. "no actual payslip"

    @property
    def issues(self) -> list[ComparisonResult]:
        return [r for r in self.results if r.is_issue]

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(Counter(r.status.value for r in self.results))

    def to_text(self, only_issues: bool = False) -> str:
        lines = [
            f"Expected vs Actual | {self.employee_code} ({self.employee_name}) | {self.period} | "
            f"{len(self.issues)} issue(s)"
        ]
        if self.note:
            lines.append(f"  {self.note}")
        header = f"  {'Component':<22} {'Expected':>14} {'Actual':>14} {'Difference':>14}  Status"
        lines.append(header)
        for r in self.results:
            if only_issues and not r.is_issue:
                continue
            lines.append(
                f"  {r.component:<22} {r.fmt(r.expected):>14} {r.fmt(r.actual):>14} "
                f"{r.fmt(r.difference):>14}  {r.status.value}"
            )
            for cause in r.root_causes:
                lines.append(f"      → [{r.issue_type.value if r.issue_type else '-'}] {cause}")
        return "\n".join(lines)
