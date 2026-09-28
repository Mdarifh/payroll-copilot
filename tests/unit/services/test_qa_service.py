from sqlalchemy import func, select

from payroll_copilot.calculations.validation import IssueType
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import QAIssue, QARun
from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll
from payroll_copilot.services.qa_service import QAService

SEP = PayrollPeriod(2026, 9)

# one finding per deliberate error in config/seed/actual_payroll_2026_09.yaml
EXPECTED_FINDINGS = {
    ("ARIF-112", IssueType.RULE_VERSION),
    ("EMP-002", IssueType.LOP),
    ("EMP-004", IssueType.ARREAR),
    ("EMP-005", IssueType.PRORATION),
    ("EMP-007", IssueType.DATA_QUALITY),
    ("EMP-008", IssueType.ROUNDING),
    ("EMP-008", IssueType.SLAB),
    ("EMP-010", IssueType.ARREAR),
}


def test_report_matches_the_injected_errors(seeded_session):
    seed_actual_payroll(seeded_session)
    report, text, _ = QAService(seeded_session).run(SEP)
    assert {(f.employee_code, f.category) for f in report.findings} == EXPECTED_FINDINGS
    s = report.summary
    assert (s["employees_checked"], s["clean"], s["with_issues"]) == (10, 3, 7)
    assert s["by_severity"] == {"HIGH": 5, "MEDIUM": 2, "LOW": 1}
    assert "Clean: EMP-003, EMP-006, EMP-009" in text
    emp010 = next(f for f in report.findings if f.employee_code == "EMP-010")
    assert emp010.lead_component == "ARREARS" and "BASIC" in emp010.components


def test_save_history_and_load(seeded_session):
    seed_actual_payroll(seeded_session)
    service = QAService(seeded_session)
    report, _, validation = service.run(SEP)
    run_id = service.save(report, validation)
    run = seeded_session.get(QARun, run_id)
    assert (run.period, run.issues_found, run.employees_checked) == ("2026-09", 8, 10)
    total = seeded_session.scalar(select(func.count()).select_from(QAIssue))
    assert total == sum(len(e.issues) for e in validation.employees) == 64
    loaded_run, issues = service.load(run_id)
    primary = [i for i in issues if i.trace["primary"]]
    assert {i.category for i in primary} >= {"RULE_VERSION", "LOP", "ARREAR", "SLAB"}
    assert all(i.trace["finding"] for i in primary)
    assert service.history()[0].id == run_id


def test_run_is_read_only(seeded_session):
    seed_actual_payroll(seeded_session)
    QAService(seeded_session).run(SEP)
    assert seeded_session.scalar(select(func.count()).select_from(QARun)) == 0


def test_no_actual_run(seeded_session):
    report, text, _ = QAService(seeded_session).run(SEP)
    assert text.startswith("No actual payroll run found for 2026-09")
    assert all(e.status == "NO_PAYSLIP" for e in report.employees)
