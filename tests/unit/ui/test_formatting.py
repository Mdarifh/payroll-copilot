from datetime import date
from decimal import Decimal

from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.domain.enums import RuleStatus, RuleType
from payroll_copilot.ui import formatting as fmt

DIRECTORY = [("ARIF-112", "Arif Sharma"), ("EMP-002", "Priya Nair")]


def trace(component="EE_PF", unit="INR") -> dict:
    return CalculationResult(
        component=component, unit=unit, employee_code="E1", period="2026-09",
        rule=RuleRef(rule_type=RuleType.PF, rule_code="EPF", version=2,
                     effective_from=date(2026, 9, 1), status=RuleStatus.UNVERIFIED),
        formula="PF wage × rate",
        steps=[TraceStep(label="PF wage", formula="min(30000, 25000)", result=Decimal(25000)),
               TraceStep(label="Days", result=Decimal(27), unit="DAYS")],
        raw_value=Decimal(3000), value=Decimal(3000),
    ).to_json_dict()  # fmt: skip


def test_inr():
    assert fmt.inr("125000") == "₹1,25,000"
    assert fmt.inr("27", "DAYS") == "27 days"
    assert fmt.inr(None) == "-" and fmt.inr("n/a") == "n/a"


def test_trace_rows_and_header():
    rows = fmt.trace_rows(trace())
    assert rows[0] == {"#": 1, "Step": "PF wage", "Formula": "min(30000, 25000)",
                       "Result": "₹25,000", "Note": ""}  # fmt: skip
    assert rows[1]["Result"] == "27 days"
    assert fmt.trace_header(trace()) == ("EE_PF · EPF v2 (2026-09-01 to open, UNVERIFIED) · "
                                         "result ₹3,000")  # fmt: skip


def test_collect_traces_nested_and_deduplicated():
    data = {"pf": {"x": 1}, "traces": [trace("EE_PF"), trace("EPS")], "again": [trace("EPS")]}
    assert [t["component"] for t in fmt.collect_traces(data)] == ["EE_PF", "EPS"]


def test_payslip_tables():
    payslip = {
        "earnings": [{"name": "Basic", "amount": "30000", "is_arrear": False},
                     {"name": "Basic", "amount": "2500", "is_arrear": True}],
        "deductions": [{"name": "Employee PF", "amount": "3000"}],
        "employer_contributions": [], "gross_earnings": "32500", "total_deductions": "3000",
        "net_pay": "29500", "total_employer_contributions": "0", "monthly_ctc": "32500",
    }  # fmt: skip
    tables = fmt.payslip_tables(payslip)
    assert tables["Earnings"][1] == {"Head": "Basic (arrear)", "Amount": "₹2,500"}
    assert {"Head": "Net pay", "Amount": "₹29,500"} in tables["Totals"]


def test_comparison_and_findings():
    validation = {"results": [
        {"component": "EE_PF", "unit": "INR", "expected": "3000", "actual": "1800",
         "difference": "-1200", "status": "MISMATCH", "issue_type": "RULE_VERSION",
         "root_causes": ["old rule"]},
        {"component": "PT", "unit": "INR", "expected": "200", "actual": "200",
         "difference": "0", "status": "MATCH", "issue_type": None, "root_causes": []},
    ]}  # fmt: skip
    rows = fmt.comparison_rows(validation)
    assert rows[0]["Status"] == "❌ MISMATCH" and rows[0]["Difference"] == "-₹1,200"
    assert len(fmt.comparison_rows(validation, only_issues=True)) == 1
    findings = [
        {"severity": "LOW", "employee_code": "E2", "category": "ROUNDING", "title": "r",
         "expected": "144", "actual": "143", "difference": "-1", "unit": "INR",
         "recommendation": "x", "lead_component": "EE_ESIC"},
        {"severity": "HIGH", "employee_code": "E1", "category": "LOP", "title": "t",
         "expected": "27", "actual": "30", "difference": "3", "unit": "DAYS",
         "recommendation": "y", "lead_component": "PAID_DAYS"},
    ]  # fmt: skip
    rows = fmt.finding_rows(findings)
    assert rows[0]["Severity"] == "🔴 HIGH" and rows[0]["Difference"] == "3 days"
    csv_text = fmt.findings_csv("2026-09", findings)
    assert csv_text.count("\n") == 3 and "\r" not in csv_text


def test_month_options():
    months = fmt.month_options("2026-09")
    assert (months[0], months[12], months[-1], len(months)) == ("2025-09", "2026-09", "2026-12", 16)


def test_default_period():
    assert fmt.default_period("2026-09") == "2026-09"
    assert fmt.default_period(None, today=date(2027, 1, 15)) == "2026-12"


class TestAddContext:
    def test_adds_employee_and_month(self):
        assert fmt.add_context("PF kitna hai?", "ARIF-112", "2026-09", DIRECTORY) == (
            "PF kitna hai? (employee ARIF-112, period 2026-09)")

    def test_keeps_named_employee(self):
        out = fmt.add_context("Priya ki salary kyun kam hui", "ARIF-112", "2026-09", DIRECTORY)
        assert "ARIF-112" not in out and out.endswith("(period 2026-09)")

    def test_keeps_explicit_code_and_period(self):
        text = "EMP-002 ka PF Sep 2026"
        assert fmt.add_context(text, "ARIF-112", "2026-09", DIRECTORY) == text

    def test_policy_question_gets_no_employee(self):
        out = fmt.add_context("What is the PF wage ceiling?", "ARIF-112", "2026-08", DIRECTORY)
        assert out == "What is the PF wage ceiling? (period 2026-08)"
