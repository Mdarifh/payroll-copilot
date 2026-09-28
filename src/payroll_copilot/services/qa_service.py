"""Run payroll QA for a period; optionally persist the run (explicit, local write)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.agent.qa_graph import build_qa_graph
from payroll_copilot.calculations.qa import QAReport
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import QAIssue, QARun
from payroll_copilot.domain.enums import QARunStatus
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.repositories.payroll_repo import PayrollRepository
from payroll_copilot.services.validation_service import ValidationService


class QAService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.graph = build_qa_graph(
            lambda: ValidationService(session), lambda: PayrollRepository(session)
        )

    def run(self, period: PayrollPeriod) -> tuple[QAReport, str, object]:
        """Returns (report, text, period validation). Read-only."""
        state = self.graph.invoke({"period": str(period)})
        return state["report"], state["text"], state["validation"]

    def save(self, report: QAReport, validation) -> int:
        """Persist the run and every component issue. Caller owns the transaction."""
        employees = {e.employee_code: e.id for e in EmployeeRepository(self.session).list_all()}
        primary = {(f.employee_code, c): f.title for f in report.findings for c in f.components}
        run = QARun(
            period=report.period, status=QARunStatus.COMPLETED,
            employees_checked=len(report.employees), issues_found=len(report.findings),
            summary=report.summary,
        )  # fmt: skip
        self.session.add(run)
        self.session.flush()
        for employee in validation.employees:
            for issue in employee.issues:
                key = (employee.employee_code, issue.component)
                self.session.add(QAIssue(
                    qa_run_id=run.id, employee_id=employees[employee.employee_code],
                    component=issue.component, expected=issue.expected, actual=issue.actual,
                    difference=issue.difference, status=issue.status,
                    category=(issue.issue_type.value if issue.issue_type else None),
                    root_cause=(issue.root_causes[0][:1000] if issue.root_causes else None),
                    trace={
                        "primary": key in primary, "finding": primary.get(key),
                        "group": issue.group.value, "causes": issue.root_causes,
                        "expected_trace": (issue.expected_trace.to_json_dict()
                                           if issue.expected_trace else None),
                    },
                ))  # fmt: skip
        self.session.flush()
        return run.id

    def history(self, limit: int = 20) -> list[QARun]:
        return list(self.session.scalars(select(QARun).order_by(QARun.id.desc()).limit(limit)))

    def load(self, run_id: int) -> tuple[QARun, list[QAIssue]]:
        run = self.session.get(QARun, run_id)
        if run is None:
            raise NotFoundError(f"QA run #{run_id} not found")
        issues = list(self.session.scalars(select(QAIssue).where(QAIssue.qa_run_id == run_id)))
        return run, issues
