"""Role -> permissions. The single place that decides who may do what (checked on the
server for every request and by the chat; the UI only hides what the server would refuse).

  Viewer             read payroll screens and reports
  Payroll Executive  + LOP, attendance, overtime (requests / changes)
  Payroll Manager    + one-off pay, salary revision, run payroll, approve changes and payroll
  Admin / HR         + settings, users and roles, knowledge base, approval switch
Employees and line managers have none of these (self-service only).
"""

from enum import StrEnum

from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import Role


class Perm(StrEnum):
    VIEW_PAYROLL = "view_payroll"  # every employee's payroll, payroll screens, reports
    EDIT_ATTENDANCE = "edit_attendance"  # LOP, attendance, overtime
    EDIT_PAY = "edit_pay"  # one-off earnings / deductions, bonus, salary revision
    RUN_PAYROLL = "run_payroll"  # start / run / validate / recalculate / roll back
    APPROVE = "approve"  # approve changes and payroll, finalize, reopen
    VIEW_AUDIT = "view_audit"  # audit timeline
    ADMIN = "admin"  # settings, users and roles, knowledge base management


_VIEWER = {Perm.VIEW_PAYROLL}
_EXECUTIVE = _VIEWER | {Perm.EDIT_ATTENDANCE, Perm.VIEW_AUDIT}
_MANAGER = _EXECUTIVE | {Perm.EDIT_PAY, Perm.RUN_PAYROLL, Perm.APPROVE}
_ALL = set(Perm)

ROLE_PERMS: dict[Role, frozenset[Perm]] = {
    Role.EMPLOYEE: frozenset(), Role.MANAGER: frozenset(),
    Role.VIEWER: frozenset(_VIEWER), Role.PAYROLL_EXECUTIVE: frozenset(_EXECUTIVE),
    Role.PAYROLL_MANAGER: frozenset(_MANAGER), Role.ADMIN: frozenset(_ALL),
    Role.HR: frozenset(_ALL),
}  # fmt: skip
ROLE_LABEL = {Role.EMPLOYEE: "Employee", Role.MANAGER: "Line manager", Role.HR: "HR (admin)",
              Role.ADMIN: "Admin", Role.PAYROLL_MANAGER: "Payroll Manager",
              Role.PAYROLL_EXECUTIVE: "Payroll Executive", Role.VIEWER: "Viewer"}  # fmt: skip
STAFF_ROLES = frozenset(r for r, p in ROLE_PERMS.items() if Perm.VIEW_PAYROLL in p)


def perms(user: Employee | Role) -> frozenset[Perm]:
    role = user if isinstance(user, Role) else user.role
    return ROLE_PERMS.get(role, frozenset())


def has(user: Employee | Role, perm: Perm) -> bool:
    return perm in perms(user)


def is_staff(user: Employee | Role) -> bool:
    """Payroll staff: may see every employee's payroll (any role with VIEW_PAYROLL)."""
    return has(user, Perm.VIEW_PAYROLL)


def roles_with(perm: Perm) -> frozenset[Role]:
    return frozenset(r for r, p in ROLE_PERMS.items() if perm in p)


def change_perm(kind: str) -> Perm:
    """Permission a payroll change needs: LOP / attendance / overtime vs pay / salary."""
    return Perm.EDIT_ATTENDANCE if kind in ("lop", "attendance", "overtime") else Perm.EDIT_PAY
