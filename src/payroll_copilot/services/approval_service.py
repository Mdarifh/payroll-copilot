"""Human approval (maker-checker) for write actions.

request()  validate payload → compute a read-only preview (impact) → PENDING approval
decide()   approver must be in APPROVERS and must not be the requester;
           reject → REJECTED; approve → execute inside a savepoint together with its
           audit entry → EXECUTED, or on any error roll back the change → FAILED.

Stale-data protection: previews record the value being changed; execution fails if
it changed after the request was made.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from payroll_copilot.audit.context import audit_context
from payroll_copilot.audit.logger import AuditLogger
from payroll_copilot.calculations.attendance import compute_paid_days
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError, PayrollCopilotError
from payroll_copilot.db.models import (
    Approval,
    Attendance,
    ESICRule,
    LWFRule,
    PFRule,
    PTRule,
    TDSOverride,
    TDSRule,
)
from payroll_copilot.domain.attendance import AttendanceRecord
from payroll_copilot.domain.enums import ActionType, ApprovalStatus, RuleStatus
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.approval_describe import describe
from payroll_copilot.services.attendance_service import paid_days_basis
from payroll_copilot.tools.schemas import EmployeePeriodArgs, PeriodArgs

RULE_MODELS = {"PF": PFRule, "ESIC": ESICRule, "PT": PTRule, "LWF": LWFRule, "TDS": TDSRule}


class ApprovalError(PayrollCopilotError):
    """The approval request or decision is not allowed in its current state."""


class ApprovalPermissionError(ApprovalError):
    """The user may not decide this approval (not an approver / maker-checker)."""


class InvalidApprovalRequest(PayrollCopilotError):
    """The requested action or its payload is invalid (maps to HTTP 422)."""


def user_of(actor: str) -> str:
    """'api:alice' / 'cli:alice' -> 'alice' (the person, whatever the channel)."""
    return actor.split(":", 1)[-1]


def _actor_for(user: str) -> str:
    """Keep the channel ('cli:'/'api:') of the current actor when it is the same person."""
    from payroll_copilot.audit.context import current_actor

    actor = current_actor()
    return actor if user_of(actor) == user_of(user) else user


# ---------------------------------------------------------------------- payloads


class TDSOverridePayload(EmployeePeriodArgs):
    amount: Decimal = Field(ge=0)
    reason: str = Field(min_length=5, max_length=500)


class AttendanceCorrectionPayload(EmployeePeriodArgs):
    lop_days: Decimal = Field(ge=0, le=31)
    reason: str = Field(min_length=5, max_length=500)


class QASavePayload(PeriodArgs):
    pass


class RuleVerifyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_code: str = Field(pattern=r"^[A-Z0-9_-]{2,32}$")
    version: int = Field(ge=1)
    evidence: str = Field(min_length=5, max_length=500, description="Notification / source")

    @field_validator("rule_code", mode="before")
    @classmethod
    def _upper(cls, value: str) -> str:
        return str(value).strip().upper()


PAYLOADS: dict[ActionType, type[BaseModel]] = {
    ActionType.TDS_OVERRIDE: TDSOverridePayload,
    ActionType.ATTENDANCE_CORRECTION: AttendanceCorrectionPayload,
    ActionType.SAVE_QA_RUN: QASavePayload,
    ActionType.VERIFY_RULE: RuleVerifyPayload,
}


class ApprovalView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    action_type: str
    description: str
    payload: dict[str, Any]
    preview: dict[str, Any] | None
    status: str
    requested_by: str
    reason: str | None
    decided_by: str | None
    comment: str | None
    result: dict[str, Any] | None
    created_at: str
    decided_at: str | None

    @classmethod
    def of(cls, a: Approval) -> "ApprovalView":
        return cls(
            id=a.id, action_type=a.action_type, payload=a.payload, preview=a.preview,
            description=describe(ActionType(a.action_type), a.payload), status=a.status.value,
            requested_by=a.requested_by, reason=a.reason, decided_by=a.decided_by,
            comment=a.comment, result=a.result, created_at=a.created_at.isoformat(),
            decided_at=a.decided_at.isoformat() if a.decided_at else None,
        )  # fmt: skip

    def to_text(self) -> str:
        lines = [f"Approval #{self.id} [{self.status}] {self.description}",
                 f"  requested by {self.requested_by}: {self.reason or '-'}"]  # fmt: skip
        if self.preview:
            lines.append("  preview: " + ", ".join(f"{k}={v}" for k, v in self.preview.items()))
        if self.decided_by:
            lines.append(f"  decided by {self.decided_by}: {self.comment or '-'}")
        if self.result:
            lines.append("  result: " + ", ".join(f"{k}={v}" for k, v in self.result.items()))
        return "\n".join(lines)


# ---------------------------------------------------------------------- service


class ApprovalService:
    """Operates on the caller's session; the caller commits (or the CLI/API scope does)."""

    def __init__(self, session: Session, approvers: list[str] | None = None) -> None:
        self.session = session
        self.approvers = approvers if approvers is not None else get_settings().approvers
        self.audit = AuditLogger(lambda: _same(session))

    # -------------------------------------------------------------- request

    def request(
        self, action: ActionType | str, payload: dict[str, Any], requested_by: str,
        reason: str | None = None,
    ) -> ApprovalView:  # fmt: skip
        try:
            action = ActionType(action)
        except ValueError as exc:
            known = ", ".join(a.value for a in ActionType)
            raise InvalidApprovalRequest(f"Unknown action '{action}'; use one of {known}") from exc
        try:
            data = PAYLOADS[action].model_validate(payload).model_dump(mode="json")
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            raise InvalidApprovalRequest(f"Invalid {action.value} request: {detail}") from exc
        preview = self._preview(action, data)
        approval = Approval(
            action_type=action.value, payload=data, preview=preview,
            status=ApprovalStatus.PENDING, requested_by=requested_by[:100],
            reason=reason or data.get("reason"),
        )  # fmt: skip
        self.session.add(approval)
        self.session.flush()
        with audit_context(actor=requested_by):
            self.audit.log("approval.request", entity="approval", entity_id=str(approval.id),
                           approval_id=approval.id, strict=True,
                           details={"action": action.value, "payload": data, "preview": preview})
        return ApprovalView.of(approval)

    # -------------------------------------------------------------- decide

    def _pending(self, approval_id: int) -> Approval:
        approval = self.session.get(Approval, approval_id)
        if approval is None:
            raise NotFoundError(f"Approval #{approval_id} not found")
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"Approval #{approval_id} is already {approval.status.value}")
        return approval

    def _check_approver(self, approval: Approval, approver: str) -> None:
        if not self.approvers:
            raise ApprovalPermissionError("No approvers configured (set APPROVERS in .env)")
        if user_of(approver) not in self.approvers:
            raise ApprovalPermissionError(f"'{user_of(approver)}' is not an approver")
        if user_of(approver) == user_of(approval.requested_by):
            raise ApprovalPermissionError(
                "Maker-checker: the requester cannot decide their own request"
            )

    def reject(self, approval_id: int, approver: str, comment: str) -> ApprovalView:
        approval = self._pending(approval_id)
        self._check_approver(approval, approver)
        if not comment or len(comment.strip()) < 3:
            raise ApprovalError("A rejection needs a comment")
        approval.status, approval.decided_by = ApprovalStatus.REJECTED, approver
        approval.decided_at, approval.comment = datetime.now(UTC), comment
        with audit_context(actor=_actor_for(approver)):
            self.audit.log("approval.reject", entity="approval", entity_id=str(approval.id),
                           approval_id=approval.id, strict=True, details={"comment": comment})
        self.session.flush()
        return ApprovalView.of(approval)

    def approve(self, approval_id: int, approver: str, comment: str | None = None) -> ApprovalView:
        approval = self._pending(approval_id)
        self._check_approver(approval, approver)
        approval.decided_by, approval.decided_at = approver, datetime.now(UTC)
        approval.comment, approval.status = comment, ApprovalStatus.APPROVED
        action = ActionType(approval.action_type)
        savepoint = self.session.begin_nested()
        try:
            with audit_context(actor=_actor_for(approver)):
                result = self._execute(action, approval)
                self.audit.log("approval.execute", entity="approval", entity_id=str(approval.id),
                               approval_id=approval.id, strict=True,
                               details={"action": action.value, "result": result})
            savepoint.commit()
            approval.status, approval.result = ApprovalStatus.EXECUTED, result
        except Exception as exc:  # noqa: BLE001 - any failure must leave data unchanged
            savepoint.rollback()
            approval.status = ApprovalStatus.FAILED
            approval.result = {"error": f"{type(exc).__name__}: {exc}"}
            with audit_context(actor=_actor_for(approver)):
                self.audit.log("approval.failed", entity="approval", entity_id=str(approval.id),
                               approval_id=approval.id, details=approval.result)
        approval.executed_at = datetime.now(UTC)
        self.session.flush()
        return ApprovalView.of(approval)

    # -------------------------------------------------------------- queries

    def get(self, approval_id: int) -> ApprovalView:
        approval = self.session.get(Approval, approval_id)
        if approval is None:
            raise NotFoundError(f"Approval #{approval_id} not found")
        return ApprovalView.of(approval)

    def list(self, status: str | None = None, limit: int = 50) -> list[ApprovalView]:
        stmt = select(Approval).order_by(Approval.id.desc()).limit(limit)
        if status:
            stmt = stmt.where(Approval.status == ApprovalStatus(status.upper()))
        return [ApprovalView.of(a) for a in self.session.scalars(stmt)]

    # -------------------------------------------------------------- previews

    def _attendance(self, code: str, period: str) -> tuple[Any, Attendance]:
        employee = EmployeeRepository(self.session).get_by_code(code)
        row = self.session.scalar(select(Attendance).where(
            Attendance.employee_id == employee.id, Attendance.period == period))  # fmt: skip
        if row is None:
            raise NotFoundError(f"No attendance for {code} in {period}")
        return employee, row

    def _rule(self, code: str, version: int):
        for rule_type, model in RULE_MODELS.items():
            row = self.session.scalar(
                select(model).where(model.rule_code == code, model.version == version))
            if row is not None:
                return rule_type, row
        raise NotFoundError(f"Rule {code} v{version} not found")

    def _preview(self, action: ActionType, p: dict[str, Any]) -> dict[str, Any]:
        match action:
            case ActionType.TDS_OVERRIDE:
                from payroll_copilot.services.tds_service import TDSService

                current = TDSService(self.session).calculate(p["employee_code"],
                                                             PayrollPeriod.parse(p["period"]))
                return {"current_monthly_tds": str(current.monthly_tds),
                        "new_monthly_tds": p["amount"],
                        "difference": str(Decimal(p["amount"]) - current.monthly_tds)}
            case ActionType.ATTENDANCE_CORRECTION:
                employee, row = self._attendance(p["employee_code"], p["period"])
                record = AttendanceRecord.model_validate(row)
                basis = paid_days_basis(employee)
                before = compute_paid_days(record, doj=employee.doj, lwd=employee.lwd, basis=basis)
                changed = record.model_copy(update={"lop_days": Decimal(p["lop_days"])})
                after = compute_paid_days(changed, doj=employee.doj, lwd=employee.lwd, basis=basis)
                return {"lop_days_before": str(row.lop_days), "lop_days_after": p["lop_days"],
                        "paid_days_before": str(before.value),
                        "paid_days_after": str(after.value)}  # fmt: skip
            case ActionType.SAVE_QA_RUN:
                from payroll_copilot.services.qa_service import QAService

                report, _, _ = QAService(self.session).run(PayrollPeriod.parse(p["period"]))
                return {"findings": report.summary["findings"],
                        "by_severity": report.summary["by_severity"]}  # fmt: skip
            case ActionType.VERIFY_RULE:
                rule_type, rule = self._rule(p["rule_code"], p["version"])
                if rule.status is not RuleStatus.UNVERIFIED:
                    raise ApprovalError(f"{p['rule_code']} v{p['version']} is {rule.status.value}")
                return {"rule_type": rule_type, "name": rule.name,
                        "status_before": rule.status.value,
                        "effective_from": rule.effective_from.isoformat()}  # fmt: skip
        return {}

    # -------------------------------------------------------------- executors

    def _execute(self, action: ActionType, approval: Approval) -> dict[str, Any]:
        p, preview = approval.payload, approval.preview or {}
        match action:
            case ActionType.TDS_OVERRIDE:
                employee = EmployeeRepository(self.session).get_by_code(p["employee_code"])
                self.session.execute(
                    update(TDSOverride)
                    .where(TDSOverride.employee_id == employee.id,
                           TDSOverride.period == p["period"], TDSOverride.active.is_(True))
                    .values(active=False)
                )  # fmt: skip
                override = TDSOverride(employee_id=employee.id, period=p["period"],
                                       amount=Decimal(p["amount"]), reason=p["reason"],
                                       approval_id=approval.id)  # fmt: skip
                self.session.add(override)
                self.session.flush()
                return {"tds_override_id": override.id, "amount": p["amount"]}
            case ActionType.ATTENDANCE_CORRECTION:
                _, row = self._attendance(p["employee_code"], p["period"])
                if str(row.lop_days) != preview.get("lop_days_before"):
                    raise ApprovalError(
                        f"Attendance changed since the request (LOP now {row.lop_days}, "
                        f"was {preview.get('lop_days_before')}); create a new request")
                row.lop_days = Decimal(p["lop_days"])
                row.source = f"APPROVAL#{approval.id}"
                self.session.flush()
                return {"lop_days": p["lop_days"], "paid_days": preview.get("paid_days_after")}
            case ActionType.SAVE_QA_RUN:
                from payroll_copilot.services.qa_service import QAService

                service = QAService(self.session)
                report, _, validation = service.run(PayrollPeriod.parse(p["period"]))
                run_id = service.save(report, validation)
                self.audit.log("qa.save", entity="qa_run", entity_id=str(run_id), strict=True,
                               approval_id=approval.id,
                               details={"period": p["period"], **report.summary})
                return {"qa_run_id": run_id, "findings": report.summary["findings"]}
            case ActionType.VERIFY_RULE:
                _, rule = self._rule(p["rule_code"], p["version"])
                if rule.status is not RuleStatus.UNVERIFIED:
                    raise ApprovalError(f"Rule is now {rule.status.value}; nothing to verify")
                rule.status = RuleStatus.VERIFIED
                rule.source_ref = f"{rule.source_ref or ''} | verified: {p['evidence']}"[:500]
                self.session.flush()
                return {"rule": f"{p['rule_code']} v{p['version']}", "status": "VERIFIED"}
        raise ApprovalError(f"No executor for {action.value}")


def _same(session: Session):
    from contextlib import nullcontext

    return nullcontext(session)
