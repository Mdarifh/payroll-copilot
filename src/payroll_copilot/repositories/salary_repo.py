"""Salary heads/templates (config) and per-employee structure rows (salary_structure)."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import date_ranges_overlap
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import SalaryStructure
from payroll_copilot.domain.enums import AssignmentReason
from payroll_copilot.domain.salary import (
    PayLine,
    SalaryAssignmentInfo,
    SalaryHeadInfo,
    StructureTemplate,
)
from payroll_copilot.services.salary_config import salary_heads, structure_templates


class SalaryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def heads(self) -> dict[str, SalaryHeadInfo]:
        return salary_heads()

    def template(self, code: str, version: int | None = None) -> StructureTemplate:
        template = structure_templates().get(code)
        if template is None or (version is not None and template.version != version):
            raise NotFoundError(f"Salary structure {code} (version {version or 'latest'}) "
                                "not found")  # fmt: skip
        return template

    def assignments_between(
        self, employee_id: int, start: date, end: date
    ) -> list[SalaryAssignmentInfo]:
        """Structure versions (rows grouped by effective_from) overlapping [start, end]."""
        rows = self.session.scalars(
            select(SalaryStructure)
            .where(SalaryStructure.employee_id == employee_id)
            .order_by(SalaryStructure.effective_from, SalaryStructure.id)
        )
        versions: dict[date, list[SalaryStructure]] = defaultdict(list)
        for row in rows:
            versions[row.effective_from].append(row)
        heads = self.heads()
        result = []
        for i, (effective_from, lines) in enumerate(sorted(versions.items())):
            effective_to = lines[0].effective_to
            if not date_ranges_overlap(effective_from, effective_to, start, end):
                continue
            components = [PayLine(head_code=r.head_code, amount=r.monthly_amount) for r in lines]
            gross = sum((c.amount for c in components if heads[c.head_code].part_of_gross),
                        Decimal(0))  # fmt: skip
            code = lines[0].template_code
            result.append(SalaryAssignmentInfo(
                structure=self.template(code) if code else StructureTemplate(
                    code="CUSTOM", version=1, name="Custom structure", components=[]),
                monthly_gross=gross, effective_from=effective_from, effective_to=effective_to,
                reason=AssignmentReason.JOINING if i == 0 else AssignmentReason.REVISION,
                components=components,
            ))  # fmt: skip
        return result
