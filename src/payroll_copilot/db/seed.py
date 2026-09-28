"""Load demo master data (company, heads, structures, employees...) from YAML.

Seeding is idempotent: if the company code already exists nothing is written.
Rules are loaded separately by ``payroll_copilot.rules.loader``.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Enum, select
from sqlalchemy.orm import Session

from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.core.money import to_decimal
from payroll_copilot.db.base import Base
from payroll_copilot.db.models import (
    Arrear,
    Attendance,
    Company,
    Employee,
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    FnFSettlement,
    SalaryHead,
    SalaryStructure,
    SalaryStructureComponent,
    TDSDeclaration,
)
from payroll_copilot.rules.loader import read_yaml

DEFAULT_SEED_FILE = PROJECT_ROOT / "config" / "seed" / "demo_company.yaml"


class SeedDataError(PayrollCopilotError):
    """Seed file is inconsistent (unknown field, components not adding up...)."""


@dataclass
class SeedReport:
    skipped: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    scenarios: dict[str, str] = field(default_factory=dict)


def _build[M: Base](model: type[M], data: dict[str, Any]) -> M:
    """Instantiate ``model`` converting enum strings; unknown keys are an error."""
    columns = model.__table__.columns
    unknown = set(data) - set(columns.keys())
    if unknown:
        raise SeedDataError(f"{model.__name__}: unknown field(s) {sorted(unknown)}")
    values = {}
    for key, value in data.items():
        col_type = columns[key].type
        if isinstance(value, str) and isinstance(col_type, Enum) and col_type.enum_class:
            value = col_type.enum_class(value)
        values[key] = value
    return model(**values)


def _add(report: SeedReport, key: str, n: int = 1) -> None:
    report.counts[key] = report.counts.get(key, 0) + n


def _salary_assignments(
    employee_code: str, items: list[dict], structures: dict[str, SalaryStructure]
) -> list[EmployeeSalaryStructure]:
    rows = []
    for item in items:
        item = dict(item)
        components: dict[str, Any] = item.pop("components")
        structure_code = item.pop("structure")
        if structure_code not in structures:
            raise SeedDataError(f"{employee_code}: unknown structure {structure_code}")
        total = sum((to_decimal(v) for v in components.values()), Decimal("0"))
        gross = to_decimal(item["monthly_gross"])
        if total != gross:
            raise SeedDataError(
                f"{employee_code}: components add up to {total}, monthly_gross is {gross}"
            )
        assignment = _build(EmployeeSalaryStructure, item)
        assignment.structure = structures[structure_code]
        assignment.components = [
            EmployeeSalaryComponent(head_code=head, monthly_amount=amount)
            for head, amount in components.items()
        ]
        rows.append(assignment)
    return rows


def seed_demo_data(session: Session, path: Path = DEFAULT_SEED_FILE) -> SeedReport:
    data = read_yaml(path)
    report = SeedReport()

    # Salary heads are master data: new heads are added even to an existing database.
    existing_heads = set(session.scalars(select(SalaryHead.code)))
    new_heads = [h for h in data["salary_heads"] if h["code"] not in existing_heads]
    for head in new_heads:
        session.add(_build(SalaryHead, head))
    if new_heads:
        _add(report, "salary_heads", len(new_heads))
    session.flush()

    company_code = data["company"]["code"]
    if session.scalar(select(Company).where(Company.code == company_code)):
        report.skipped = True
        return report

    structures: dict[str, SalaryStructure] = {}
    for item in data["salary_structures"]:
        item = dict(item)
        components = item.pop("components")
        structure = _build(SalaryStructure, item)
        structure.components = [_build(SalaryStructureComponent, c) for c in components]
        session.add(structure)
        structures[structure.code] = structure
    _add(report, "salary_structures", len(structures))

    company = _build(Company, data["company"])
    session.add(company)
    _add(report, "companies")

    for item in data["employees"]:
        item = dict(item)
        code = item["employee_code"]
        report.scenarios[code] = item.pop("scenario", "")
        salary = item.pop("salary", [])
        attendance = item.pop("attendance", [])
        arrears = item.pop("arrears", [])
        fnf = item.pop("fnf", None)
        declarations = item.pop("tds_declarations", [])

        employee = _build(Employee, item)
        employee.company = company
        employee.salary_assignments = _salary_assignments(code, salary, structures)
        session.add(employee)
        session.flush()
        _add(report, "employees")
        _add(report, "salary_assignments", len(salary))

        for row in attendance:
            PayrollPeriod.parse(row["period"])  # validates format
            session.add(_build(Attendance, {**row, "employee_id": employee.id}))
        _add(report, "attendance", len(attendance))

        for row in arrears:
            session.add(_build(Arrear, {**row, "employee_id": employee.id}))
        _add(report, "arrears", len(arrears))

        if fnf:
            session.add(_build(FnFSettlement, {**fnf, "employee_id": employee.id}))
            _add(report, "fnf_settlements")

        for row in declarations:
            session.add(_build(TDSDeclaration, {**row, "employee_id": employee.id}))
        _add(report, "tds_declarations", len(declarations))

    session.flush()
    return report
