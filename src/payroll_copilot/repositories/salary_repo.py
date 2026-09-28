from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from payroll_copilot.core.dates import date_ranges_overlap
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import EmployeeSalaryStructure, SalaryHead, SalaryStructure
from payroll_copilot.domain.salary import (
    PayLine,
    SalaryAssignmentInfo,
    SalaryHeadInfo,
    StructureTemplate,
)


def _template(structure: SalaryStructure) -> StructureTemplate:
    return StructureTemplate.model_validate(structure)


def _assignment(row: EmployeeSalaryStructure) -> SalaryAssignmentInfo:
    return SalaryAssignmentInfo(
        structure=_template(row.structure),
        monthly_gross=row.monthly_gross,
        annual_ctc=row.annual_ctc,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        reason=row.reason,
        components=[
            PayLine(head_code=c.head_code, amount=c.monthly_amount) for c in row.components
        ],
    )


class SalaryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def heads(self) -> dict[str, SalaryHeadInfo]:
        rows = self.session.scalars(select(SalaryHead).order_by(SalaryHead.display_order))
        return {row.code: SalaryHeadInfo.model_validate(row) for row in rows}

    def template(self, code: str, version: int | None = None) -> StructureTemplate:
        stmt = (
            select(SalaryStructure)
            .where(SalaryStructure.code == code)
            .options(selectinload(SalaryStructure.components))
            .order_by(SalaryStructure.version.desc())
        )
        if version is not None:
            stmt = stmt.where(SalaryStructure.version == version)
        row = self.session.scalars(stmt).first()
        if row is None:
            which = version or "latest"
            raise NotFoundError(f"Salary structure {code} (version {which}) not found")
        return _template(row)

    def assignments_between(
        self, employee_id: int, start: date, end: date
    ) -> list[SalaryAssignmentInfo]:
        """Assignments overlapping [start, end], oldest first."""
        rows = self.session.scalars(
            select(EmployeeSalaryStructure)
            .where(EmployeeSalaryStructure.employee_id == employee_id)
            .options(
                selectinload(EmployeeSalaryStructure.components),
                selectinload(EmployeeSalaryStructure.structure).selectinload(
                    SalaryStructure.components
                ),
            )
            .order_by(EmployeeSalaryStructure.effective_from)
        )
        return [
            _assignment(row)
            for row in rows
            if date_ranges_overlap(row.effective_from, row.effective_to, start, end)
        ]
