from decimal import Decimal

from payroll_copilot.calculations.qa import Severity, build_employee_qa, build_report
from payroll_copilot.calculations.validation import (
    ComponentGroup as G,
)
from payroll_copilot.calculations.validation import (
    EmployeeValidation,
    IssueType,
    compare,
)

D = Decimal


def r(component, group, expected, actual, issue=None, cause="cause", unit="INR"):
    result = compare("E1", "2026-09", component, group, D(expected), D(actual), unit=unit)
    return result.with_diagnosis(issue, [cause]) if issue else result


def validation(*results, note=None):
    return EmployeeValidation(employee_code="E1", employee_name="Test", period="2026-09",
                              results=list(results), note=note)  # fmt: skip


def test_lop_groups_attendance_earnings_and_effects():
    v = validation(
        r("PAID_DAYS", G.ATTENDANCE, 27, 30, IssueType.LOP, "LOP of 3 days not deducted", "DAYS"),
        r("BASIC", G.EARNINGS, 18000, 20000, IssueType.PRORATION),
        r("HRA", G.EARNINGS, 9000, 10000, IssueType.PRORATION),
        r("GROSS", G.TOTALS, 36000, 40000, IssueType.LOP),
        r("EE_PF", G.PF, 2160, 2400, IssueType.UPSTREAM),
        r("NET_PAY", G.TOTALS, 33640, 37400, IssueType.UPSTREAM),
        r("PT", G.PT, 200, 200),
    )
    qa = build_employee_qa(v)
    (finding,) = qa.findings
    assert finding.category is IssueType.LOP and finding.lead_component == "PAID_DAYS"
    assert finding.components == ["PAID_DAYS", "BASIC", "HRA"]
    assert finding.effects == ["GROSS", "EE_PF", "NET_PAY"]
    assert finding.severity is Severity.HIGH  # net impact 3760 ≥ 1000
    assert qa.net_pay_difference == D(3760) and qa.status == "ISSUES" and qa.issue_count == 6


def test_rule_version_collapses_pf_components():
    v = validation(*(r(c, G.PF, e, a, IssueType.RULE_VERSION, "old rule")
                     for c, e, a in [("EE_PF", 3000, 1800), ("EPS", 2083, 1250)]),
                   r("NET_PAY", G.TOTALS, 56800, 58000, IssueType.UPSTREAM))  # fmt: skip
    (finding,) = build_employee_qa(v).findings
    assert finding.lead_component == "EE_PF" and finding.components == ["EE_PF", "EPS"]
    assert finding.severity is Severity.HIGH and "rule version" in finding.recommendation


def test_several_independent_findings_and_severities():
    v = validation(
        r("EE_ESIC", G.ESIC, 144, 143, IssueType.ROUNDING),
        r("PT", G.PT, 150, 200, IssueType.SLAB),
        r("NET_PAY", G.TOTALS, 100, 51, IssueType.UPSTREAM),
    )
    severities = {f.category: f.severity for f in build_employee_qa(v).findings}
    assert severities == {IssueType.ROUNDING: Severity.LOW, IssueType.SLAB: Severity.MEDIUM}


def test_data_quality_merges_net():
    v = validation(r("HEADER_NET", G.DATA_QUALITY, 79800, 79700, IssueType.DATA_QUALITY),
                   r("NET_PAY", G.TOTALS, 79800, 79700, IssueType.DATA_QUALITY))  # fmt: skip
    (finding,) = build_employee_qa(v).findings
    assert finding.components == ["HEADER_NET", "NET_PAY"] and finding.severity is Severity.HIGH


def test_amount_based_severity_for_arrears():
    small = validation(r("ARREARS", G.ARREAR, 775, 0, IssueType.ARREAR))
    large = validation(r("ARREARS", G.ARREAR, 5000, 0, IssueType.ARREAR))
    tiny = validation(r("ARREARS", G.ARREAR, 50, 0, IssueType.ARREAR))
    assert build_employee_qa(small).findings[0].severity is Severity.MEDIUM
    assert build_employee_qa(large).findings[0].severity is Severity.HIGH
    assert build_employee_qa(tiny).findings[0].severity is Severity.LOW


def test_clean_and_missing_payslip():
    assert build_employee_qa(validation(r("PT", G.PT, 200, 200))).status == "CLEAN"
    missing = validation(r("PAYSLIP", G.TOTALS, 100, 0, IssueType.MISSING),
                         note="No actual payslip found")  # fmt: skip
    assert build_employee_qa(missing).status == "NO_PAYSLIP"


def test_report_outputs():
    a = validation(r("EE_PF", G.PF, 3000, 1800, IssueType.RULE_VERSION, "old rule"),
                   r("NET_PAY", G.TOTALS, 100, 1300, IssueType.UPSTREAM))  # fmt: skip
    b = EmployeeValidation(employee_code="E2", employee_name="B", period="2026-09",
                           results=[r("ARREARS", G.ARREAR, 500, 0, IssueType.ARREAR),
                                    r("NET_PAY", G.TOTALS, 1000, 500, IssueType.UPSTREAM)])  # fmt: skip
    report = build_report("2026-09", [a, b])
    s = report.summary
    assert (s["employees_checked"], s["with_issues"], s["findings"]) == (2, 2, 2)
    assert (s["net_overpaid"], s["net_underpaid"]) == ("1200", "500")
    assert report.statutory_impact == {"PF": D(-1200)}
    text = report.to_text()
    assert text.index("[HIGH  ] E1") < text.index("E2")
    assert "| HIGH | E1 | RULE_VERSION | old rule |" in report.to_markdown()
    csv_text = report.to_csv()
    assert csv_text.splitlines()[0].startswith("period,employee_code") and "E2" in csv_text
