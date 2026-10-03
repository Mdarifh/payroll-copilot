"""Check email + password and issue an access token. Every attempt is audited."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import can_log_in
from payroll_copilot.auth.security import burn_password_check, issue_token, verify_password
from payroll_copilot.core.exceptions import AuthError
from payroll_copilot.db.models import Employee
from payroll_copilot.services import audit

INVALID_LOGIN = "Invalid email or password"  # same message for every failure


def login(
    session: Session, email: str, password: str, request_id: str | None = None
) -> tuple[Employee, str, datetime]:
    email = email.strip().lower()
    employee = session.scalar(select(Employee).where(Employee.email == email))
    if employee is None:
        burn_password_check(password)
        ok = False
    else:
        ok = verify_password(password, employee.password_hash) and can_log_in(employee)
    if not ok:
        audit.record(session, "auth.login_failed", actor=None, entity="employee",
                     entity_id=employee.id if employee else None,
                     new={"email": email}, request_id=request_id)  # fmt: skip
        session.commit()  # keep the failed attempt even though the request fails
        raise AuthError(INVALID_LOGIN)
    token, expires = issue_token(employee.id, employee.role)
    audit.record(session, "auth.login", actor=employee, entity="employee",
                 entity_id=employee.id, request_id=request_id)  # fmt: skip
    return employee, token, expires
