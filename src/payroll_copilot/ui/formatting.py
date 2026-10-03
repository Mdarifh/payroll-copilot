"""Pure helpers turning API JSON into table rows for the UI (no Streamlit import)."""

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from payroll_copilot.core.money import format_inr

STATUS_ICON = {
    "MATCH": "✅ MATCH", "WITHIN_TOLERANCE": "🟡 WITHIN TOLERANCE", "MISMATCH": "❌ MISMATCH",
    "MISSING_ACTUAL": "⚠️ MISSING ACTUAL", "MISSING_EXPECTED": "⚠️ UNEXPECTED",
}  # fmt: skip
SEVERITY_ICON = {"HIGH": "🔴 HIGH", "MEDIUM": "🟠 MEDIUM", "LOW": "🟢 LOW"}


def inr(value: Any, unit: str = "INR") -> str:
    """Format a money string/number for display ('3000' -> '₹3,000')."""
    if value is None or value == "":
        return "-"
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    if unit == "DAYS":
        return f"{amount.normalize():f} days"
    return format_inr(amount)


def trace_rows(trace: dict) -> list[dict]:
    unit = trace.get("unit", "INR")
    rows = []
    for number, step in enumerate(trace.get("steps", []), start=1):
        rows.append({
            "#": number,
            "Step": step.get("label", ""),
            "Formula": step.get("formula") or "",
            "Result": inr(step.get("result"), step.get("unit") or unit)
            if step.get("result") is not None else "",
            "Note": step.get("note") or "",
        })  # fmt: skip
    return rows


def trace_header(trace: dict) -> str:
    rule = trace.get("rule")
    head = trace.get("component", "")
    if rule:
        until = rule.get("effective_to") or "open"
        head += f" · {rule['rule_code']} v{rule['version']} ({rule['effective_from']} to {until}"
        head += f", {rule['status']})"
    return head + f" · result {inr(trace.get('value'), trace.get('unit', 'INR'))}"


def collect_traces(data: Any) -> list[dict]:
    """All CalculationResult-shaped dicts found in an API ``data`` payload."""
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if {"component", "steps", "value", "formula"} <= node.keys():
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    unique, seen = [], set()
    for trace in found:
        key = (trace["component"], trace.get("employee_code"), trace.get("period"))
        if key not in seen:
            seen.add(key)
            unique.append(trace)
    return unique


def payslip_tables(payslip: dict) -> dict[str, list[dict]]:
    def lines(items: list[dict]) -> list[dict]:
        return [{"Head": i["name"] + (" (arrear)" if i.get("is_arrear") else ""),
                 "Amount": inr(i["amount"])} for i in items]  # fmt: skip

    return {
        "Earnings": lines(payslip.get("earnings", [])),
        "Employee deductions": lines(payslip.get("deductions", [])),
        "Employer contributions": lines(payslip.get("employer_contributions", [])),
        "Totals": [
            {"Head": "Gross earnings", "Amount": inr(payslip.get("gross_earnings"))},
            {"Head": "Total deductions", "Amount": inr(payslip.get("total_deductions"))},
            {"Head": "Net pay", "Amount": inr(payslip.get("net_pay"))},
            {"Head": "Employer contributions", "Amount":
                inr(payslip.get("total_employer_contributions"))},
            {"Head": "Monthly CTC", "Amount": inr(payslip.get("monthly_ctc"))},
        ],
    }  # fmt: skip


def comparison_rows(validation: dict, only_issues: bool = False) -> list[dict]:
    rows = []
    for r in validation.get("results", []):
        is_issue = r["status"] in ("MISMATCH", "MISSING_ACTUAL", "MISSING_EXPECTED")
        if only_issues and not is_issue:
            continue
        unit = r.get("unit", "INR")
        rows.append({
            "Component": r["component"],
            "Expected": inr(r.get("expected"), unit),
            "Actual": inr(r.get("actual"), unit),
            "Difference": inr(r.get("difference"), unit),
            "Status": STATUS_ICON.get(r["status"], r["status"]),
            "Issue type": r.get("issue_type") or "",
            "Root cause": "; ".join(r.get("root_causes") or []),
        })  # fmt: skip
    return rows


def finding_rows(findings: list[dict]) -> list[dict]:
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    rows = []
    for f in sorted(findings, key=lambda f: (order.get(f["severity"], 3), f["employee_code"])):
        unit = f.get("unit", "INR")
        rows.append({
            "Severity": SEVERITY_ICON.get(f["severity"], f["severity"]),
            "Employee": f["employee_code"],
            "Category": f["category"],
            "Finding": f["title"],
            "Expected": inr(f.get("expected"), unit),
            "Actual": inr(f.get("actual"), unit),
            "Difference": inr(f.get("difference"), unit),
            "Recommendation": f["recommendation"],
        })  # fmt: skip
    return rows


def findings_csv(period: str, findings: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["period", "employee_code", "severity", "category", "lead_component",
                     "expected", "actual", "difference", "title", "recommendation"])  # fmt: skip
    for f in findings:
        writer.writerow([period, f["employee_code"], f["severity"], f["category"],
                         f["lead_component"], f.get("expected"), f.get("actual"),
                         f.get("difference"), f["title"], f["recommendation"]])  # fmt: skip
    return buffer.getvalue()


def add_context(prompt: str, employee_code: str | None, period: str | None,
                directory: list[tuple[str, str]]) -> str:  # fmt: skip
    """Append the sidebar employee / month to a chat question when it names neither.

    Never adds an employee to a policy question (that would turn it into a
    calculation) or when the question already names an employee by code or name.
    """
    from payroll_copilot.agent.entities import extract_entities, extract_period
    from payroll_copilot.agent.intents import Intent, detect_intent

    entities = extract_entities(prompt, directory)
    names_someone = entities.employee_code is not None or any(
        "several employees" in n or "not found" in n for n in entities.notes
    )
    extra = []
    policy_question = detect_intent(prompt, False)[0] is Intent.KNOWLEDGE
    if employee_code and not names_someone and not policy_question:
        extra.append(f"employee {employee_code}")
    if period and extract_period(prompt) is None:
        extra.append(f"period {period}")
    return f"{prompt} ({', '.join(extra)})" if extra else prompt


def default_period(latest_period: str | None, today: date | None = None) -> str:
    """Month with the newest data, else the previous calendar month (payroll is run after it)."""
    from payroll_copilot.core.dates import PayrollPeriod

    if latest_period:
        return str(PayrollPeriod.parse(latest_period))
    return str(PayrollPeriod.from_date(today or date.today()).previous())


def month_options(center: str, before: int = 12, after: int = 3) -> list[str]:
    """``before`` months up to ``center``, then ``after`` months (arrears / projections)."""
    from payroll_copilot.core.dates import PayrollPeriod

    current = PayrollPeriod.parse(center)
    for _ in range(before):
        current = current.previous()
    months = []
    for _ in range(before + 1 + after):
        months.append(str(current))
        current = current.next()
    return months
