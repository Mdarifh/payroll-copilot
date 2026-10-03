"""FastAPI dependencies: database session, logged-in user and role checks.

Role checks live here, in the backend - never only in the LLM prompt.
"""

from collections.abc import Callable, Iterator

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from payroll_copilot.auth.security import decode_token
from payroll_copilot.core.exceptions import AuthError, PermissionDeniedError
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import EmployeeStatus, Role

# auto_error=False: we raise our own AuthError so every 401 has the same shape
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def get_session(request: Request) -> Iterator[Session]:
    """One transaction per request: commit on success, roll back on any error."""
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def can_log_in(employee: Employee | None) -> bool:
    return bool(employee and employee.login_enabled and employee.status is EmployeeStatus.ACTIVE)


def current_user(
    token: str | None = Depends(oauth2_scheme), session: Session = Depends(get_session)
) -> Employee:
    if not token:
        raise AuthError("Not logged in")
    data = decode_token(token)
    employee = session.get(Employee, data.employee_id)
    if not can_log_in(employee):
        raise AuthError("Account disabled or not found")
    if employee.role is not data.role:  # role changed after login: log in again
        raise AuthError("Role changed, please log in again")
    return employee


def require_role(*roles: Role) -> Callable[[Employee], Employee]:
    def check(user: Employee = Depends(current_user)) -> Employee:
        if user.role not in roles:
            raise PermissionDeniedError(
                f"This needs the {' or '.join(r.value for r in roles)} role"
            )
        return user

    return check


def require_permission(perm: "Perm") -> Callable[[Employee], Employee]:
    from payroll_copilot.auth.permissions import ROLE_LABEL, has, roles_with

    def check(user: Employee = Depends(current_user)) -> Employee:
        if not has(user, perm):
            allowed = ", ".join(sorted(ROLE_LABEL[r] for r in roles_with(perm)))
            raise PermissionDeniedError(f"Your role cannot do this (needs: {allowed})")
        return user

    return check


# payroll staff with read access (every staff role); writes use require_permission(...)
from payroll_copilot.auth.permissions import Perm  # noqa: E402

require_hr = require_permission(Perm.VIEW_PAYROLL)
require_edit_attendance = require_permission(Perm.EDIT_ATTENDANCE)
require_edit_pay = require_permission(Perm.EDIT_PAY)
require_run_payroll = require_permission(Perm.RUN_PAYROLL)
require_approve = require_permission(Perm.APPROVE)
require_admin = require_permission(Perm.ADMIN)
require_audit = require_permission(Perm.VIEW_AUDIT)
