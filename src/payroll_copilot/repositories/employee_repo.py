from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import Employee


class EmployeeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_code(self, employee_code: str) -> Employee:
        employee = self.session.scalar(
            select(Employee).where(Employee.employee_code == employee_code.strip().upper())
        )
        if employee is None:
            raise NotFoundError(f"Employee {employee_code} not found")
        return employee

    def list_all(self) -> list[Employee]:
        return list(self.session.scalars(select(Employee).order_by(Employee.employee_code)))
