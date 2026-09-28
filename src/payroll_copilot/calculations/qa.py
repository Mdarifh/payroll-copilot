"""Payroll QA report: group expected-vs-actual issues into findings.

Phase 11 reports every mismatching component. One real error (e.g. LOP not
deducted) shows up in paid days, gross, each earning head, PF and net pay. QA
groups them into *findings*: one primary cause per employee and category, with
the dependent mismatches attached as effects, a severity and a recommendation.
"""

import csv
import io
from collections import Counter
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.validation import (
    ComparisonResult,
    ComponentGroup,
    EmployeeValidation,
    IssueType,
)
from payroll_copilot.core.money import format_inr

ZERO = Decimal(0)
TOTALS = {"GROSS", "NET_PAY", "TOTAL_DEDUCTIONS", "TOTAL_EMPLOYER"}
STATUTORY = {"EE_PF": "PF", "ER_EPF": "PF", "EPS": "PF", "EDLI": "PF", "EPF_ADMIN": "PF",
             "EE_ESIC": "ESIC", "ER_ESIC": "ESIC", "PT": "PT", "TDS": "TDS", "LWF_EE": "LWF",
             "LWF_ER": "LWF"}  # fmt: skip
# which component leads a finding (first match wins)
_LEAD_ORDER = ["PAID_DAYS", "LOP_DAYS", "ARREARS", "EE_PF", "EE_ESIC", "PT", "TDS", "HEADER_NET",
               "HEADER_GROSS", "HEADER_DEDUCTIONS", "GROSS"]  # fmt: skip


class Severity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


_RECOMMENDATION = {
    IssueType.RULE_VERSION: "Apply the rule version effective for the period in the payroll "
    "system and reprocess the affected employees.",
    IssueType.CEILING: "Check the wage ceiling configured in the payroll system.",
    IssueType.LOP: "Sync attendance/LOP into payroll before processing and recompute.",
    IssueType.PRORATION: "Check DOJ/LWD and paid-days proration in the payroll system.",
    IssueType.ARREAR: "Pay the pending arrear / LOP reversal (next run or off-cycle).",
    IssueType.ROUNDING: "Align the rounding mode with the rule (e.g. ESIC rounds up).",
    IssueType.SLAB: "Correct the PT slab configuration for the employee's state.",
    IssueType.ELIGIBILITY: "Check the employee's applicability flags for this component.",
    IssueType.DATA_QUALITY: "Payslip totals do not match their lines; fix before disbursement.",
    IssueType.MISSING: "Generate the missing payslip / component.",
    IssueType.UNKNOWN: "Investigate using the expected calculation trace.",
    IssueType.UPSTREAM: "Fix the primary finding; this follows from it.",
}  # fmt: skip


class QAFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    employee_name: str
    category: IssueType
    severity: Severity
    title: str
    lead_component: str
    expected: Decimal | None
    actual: Decimal | None
    difference: Decimal | None
    unit: str
    components: list[str]
    effects: list[str]
    recommendation: str


class EmployeeQA(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    employee_name: str
    status: str  # CLEAN | ISSUES | NO_PAYSLIP
    net_pay_difference: Decimal = ZERO  # actual − expected (positive = overpaid)
    employer_difference: Decimal = ZERO
    findings: list[QAFinding] = Field(default_factory=list)
    issue_count: int = 0


class QAReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str
    employees: list[EmployeeQA]
    statutory_impact: dict[str, Decimal]  # component group -> Σ (actual − expected)
    run_id: int | None = None

    @property
    def findings(self) -> list[QAFinding]:
        return [f for e in self.employees for f in e.findings]

    @property
    def summary(self) -> dict:
        employees = self.employees
        overpaid = sum((e.net_pay_difference for e in employees if e.net_pay_difference > 0), ZERO)
        underpaid = sum((e.net_pay_difference for e in employees if e.net_pay_difference < 0), ZERO)
        return {
            "period": self.period,
            "employees_checked": len(employees),
            "clean": sum(1 for e in employees if e.status == "CLEAN"),
            "with_issues": sum(1 for e in employees if e.status != "CLEAN"),
            "findings": len(self.findings),
            "component_issues": sum(e.issue_count for e in employees),
            "by_category": dict(Counter(f.category.value for f in self.findings)),
            "by_severity": dict(Counter(f.severity.value for f in self.findings)),
            "net_overpaid": str(overpaid),
            "net_underpaid": str(-underpaid),
            "statutory_impact": {k: str(v) for k, v in self.statutory_impact.items()},
        }

    def to_text(self) -> str:
        s = self.summary
        lines = [
            f"Payroll QA report | {self.period}"
            + (f" | run #{self.run_id}" if self.run_id else ""),
            f"  Employees checked: {s['employees_checked']} | clean: {s['clean']} | "
            f"with issues: {s['with_issues']}",
            f"  Findings: {s['findings']} (from {s['component_issues']} component mismatches) | "
            f"by severity {s['by_severity']}",
            f"  By category: {s['by_category']}",
            f"  Net pay overpaid {format_inr(Decimal(s['net_overpaid']))} | underpaid "
            f"{format_inr(Decimal(s['net_underpaid']))}",
        ]
        if self.statutory_impact:
            impact = ", ".join(f"{k} {format_inr(v)}" for k, v in self.statutory_impact.items())
            lines.append(f"  Statutory impact (actual − expected): {impact}")
        lines.append("")
        order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
        for f in sorted(self.findings, key=lambda f: (order[f.severity], f.employee_code)):
            lines.append(f"  [{f.severity.value:<6}] {f.employee_code:<9} {f.category.value:<12} "
                         f"{f.title}")  # fmt: skip
            diff = _fmt(f.difference, f.unit)
            lines.append(f"      {f.lead_component}: expected {_fmt(f.expected, f.unit)}, actual "
                         f"{_fmt(f.actual, f.unit)}, difference {diff}")  # fmt: skip
            if f.effects:
                lines.append(f"      effects: {', '.join(f.effects)}")
            lines.append(f"      → {f.recommendation}")
        clean = [e.employee_code for e in self.employees if e.status == "CLEAN"]
        if clean:
            lines.append(f"\n  Clean: {', '.join(clean)}")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        s = self.summary
        out = [
            f"# Payroll QA report - {self.period}", "",
            f"- Employees checked: **{s['employees_checked']}** (clean {s['clean']}, "
            f"with issues {s['with_issues']})",
            f"- Findings: **{s['findings']}** from {s['component_issues']} component mismatches",
            f"- By severity: {s['by_severity']}", f"- By category: {s['by_category']}",
            f"- Net pay overpaid ₹{s['net_overpaid']}, underpaid ₹{s['net_underpaid']}", "",
            "| Severity | Employee | Category | Finding | Expected | Actual | Difference | "
            "Recommendation |",
            "|---|---|---|---|---|---|---|---|",
        ]  # fmt: skip
        for f in self.findings:
            out.append(
                f"| {f.severity.value} | {f.employee_code} | {f.category.value} | {f.title} | "
                f"{_fmt(f.expected, f.unit)} | {_fmt(f.actual, f.unit)} | "
                f"{_fmt(f.difference, f.unit)} | {f.recommendation} |"
            )
        return "\n".join(out) + "\n"

    def to_csv(self) -> str:
        buffer = io.StringIO()
        # "\n": the text-mode file write adds the platform newline (avoids blank rows on Windows)
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["period", "employee_code", "employee_name", "severity", "category",
                         "lead_component", "expected", "actual", "difference", "title",
                         "components", "effects", "recommendation"])  # fmt: skip
        for f in self.findings:
            writer.writerow([self.period, f.employee_code, f.employee_name, f.severity.value,
                             f.category.value, f.lead_component, f.expected, f.actual,
                             f.difference, f.title, ";".join(f.components), ";".join(f.effects),
                             f.recommendation])  # fmt: skip
        return buffer.getvalue()


def _fmt(value: Decimal | None, unit: str) -> str:
    if value is None:
        return "-"
    return f"{value.normalize():f} days" if unit == "DAYS" else format_inr(value)


def _severity(category: IssueType, amount: Decimal) -> Severity:
    if category in (IssueType.RULE_VERSION, IssueType.DATA_QUALITY, IssueType.ELIGIBILITY,
                    IssueType.MISSING):  # fmt: skip
        return Severity.HIGH
    if category is IssueType.ROUNDING:
        return Severity.LOW
    if category is IssueType.SLAB:
        return Severity.MEDIUM
    if amount >= 1000:
        return Severity.HIGH
    return Severity.MEDIUM if amount >= 100 else Severity.LOW


def _lead(results: list[ComparisonResult]) -> ComparisonResult:
    for name in _LEAD_ORDER:
        for r in results:
            if r.component == name:
                return r
    return results[0]


def build_employee_qa(validation: EmployeeValidation) -> EmployeeQA:
    issues = validation.issues
    if validation.note == "No actual payslip found":
        status = "NO_PAYSLIP"
    else:
        status = "ISSUES" if issues else "CLEAN"
    by_component = {r.component: r for r in validation.results}
    net = by_component.get("NET_PAY")
    employer = by_component.get("TOTAL_EMPLOYER")

    primary = [
        r for r in issues
        if r.issue_type is not IssueType.UPSTREAM
        and (r.component not in TOTALS or r.issue_type is IssueType.DATA_QUALITY
             or len([x for x in issues if x.component not in TOTALS]) == 0)
    ]  # fmt: skip
    has_attendance = any(r.group is ComponentGroup.ATTENDANCE for r in primary)
    groups: dict[IssueType, list[ComparisonResult]] = {}
    for r in primary:
        category = r.issue_type or IssueType.UNKNOWN
        is_earning_proration = (
            r.group is ComponentGroup.EARNINGS and category is IssueType.PRORATION
        )
        if has_attendance and is_earning_proration:
            attendance_type = next(x.issue_type for x in primary
                                   if x.group is ComponentGroup.ATTENDANCE)  # fmt: skip
            category = attendance_type or category
        if r.component == "NET_PAY" and r.issue_type is IssueType.DATA_QUALITY:
            category = IssueType.DATA_QUALITY
        groups.setdefault(category, []).append(r)

    in_findings = {r.component for rs in groups.values() for r in rs}
    effects = [r.component for r in issues if r.component not in in_findings]
    findings = []
    for category, results in groups.items():
        lead = _lead(results)
        amount = abs(lead.difference or ZERO) if lead.unit == "INR" else abs(
            net.difference if net and net.difference else ZERO)  # fmt: skip
        title = lead.root_causes[0] if lead.root_causes else f"{lead.component} mismatch"
        findings.append(QAFinding(
            employee_code=validation.employee_code, employee_name=validation.employee_name,
            category=category, severity=_severity(category, amount), title=title,
            lead_component=lead.component, expected=lead.expected, actual=lead.actual,
            difference=lead.difference, unit=lead.unit,
            components=[r.component for r in results], effects=effects,
            recommendation=_RECOMMENDATION[category],
        ))  # fmt: skip
    return EmployeeQA(
        employee_code=validation.employee_code, employee_name=validation.employee_name,
        status=status,
        net_pay_difference=(net.difference if net and net.difference is not None else ZERO),
        employer_difference=(employer.difference if employer and employer.difference is not None
                             else ZERO),
        findings=findings, issue_count=len(issues),
    )  # fmt: skip


def build_report(period: str, validations: list[EmployeeValidation]) -> QAReport:
    employees = [build_employee_qa(v) for v in validations]
    impact: dict[str, Decimal] = {}
    for v in validations:
        for r in v.results:
            group = STATUTORY.get(r.component)
            if group and r.difference:
                impact[group] = impact.get(group, ZERO) + r.difference
    return QAReport(period=period, employees=employees, statutory_impact=impact)
