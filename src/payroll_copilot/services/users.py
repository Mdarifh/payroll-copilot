"""Users and roles (Settings, Admin only): change a user's role or turn their login on/off.

Guards: nobody changes their own role or login (no self-lock-out), and the company always
keeps at least one active user with the ADMIN permission. A role change takes effect at
the user's next request (their token carries the old role and is refused).
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.auth.permissions import ROLE_LABEL, Perm, perms, roles_with
from payroll_copilot.core.exceptions import (
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import Role
from payroll_copilot.services import audit


def list_users(session: Session, search: str | None = None) -> dict[str, Any]:
    stmt = select(Employee).order_by(Employee.full_name)
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(Employee.full_name.ilike(like) | Employee.employee_code.ilike(like)
                          | Employee.email.ilike(like))
    users = [{"employee_code": e.employee_code, "full_name": e.full_name, "email": e.email,
              "department": e.department, "role": e.role.value,
              "role_label": ROLE_LABEL[e.role], "login_enabled": e.login_enabled,
              "permissions": sorted(p.value for p in perms(e))}
             for e in session.scalars(stmt)]  # fmt: skip
    return {"users": users, "roles": [{"role": r.value, "label": ROLE_LABEL[r],
                                       "permissions": sorted(p.value for p in perms(r))}
                                      for r in Role]}  # fmt: skip


def update_user(session: Session, actor: Employee, code: str, role: str | None,
                login_enabled: bool | None, reason: str) -> dict[str, Any]:  # fmt: skip
    if len((reason or "").strip()) < 5:
        raise PayrollCopilotError("A reason (at least 5 characters) is required")
    user = session.scalar(select(Employee).where(Employee.employee_code == code.upper()))
    if user is None:
        raise NotFoundError(f"Employee {code} not found")
    if user.id == actor.id:
        raise PermissionDeniedError("You cannot change your own role or login")
    old = {"role": user.role.value, "login_enabled": user.login_enabled}
    new_role = Role(role) if role else user.role
    new_login = user.login_enabled if login_enabled is None else login_enabled
    admins = set(session.scalars(select(Employee.id).where(
        Employee.role.in_(roles_with(Perm.ADMIN)), Employee.login_enabled.is_(True))))
    loses_admin = user.id in admins and (Perm.ADMIN not in perms(new_role) or not new_login)
    if loses_admin and admins == {user.id}:
        raise PayrollCopilotError("At least one active Admin must remain")
    user.role, user.login_enabled = new_role, new_login
    changes = {k: v for k, v in {"role": new_role.value, "login_enabled": new_login}.items()
               if old[k] != v}
    if not changes:
        raise PayrollCopilotError("Nothing to change")
    audit.record(session, "user.update", actor=actor, entity="employee",
                 entity_id=user.employee_code, old={k: old[k] for k in changes},
                 new={**changes, "reason": reason.strip()})  # fmt: skip
    session.flush()
    return {"employee_code": user.employee_code, "role": new_role.value,
            "role_label": ROLE_LABEL[new_role], "login_enabled": new_login}
