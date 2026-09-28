from datetime import date
from decimal import Decimal

import pytest

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import EmployeeSalaryComponent, EmployeeSalaryStructure
from payroll_copilot.domain.enums import AssignmentReason
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.salary_structure_service import SalaryStructureService

SEP = PayrollPeriod(2026, 9)


def _view(session, code, period=SEP):
    return SalaryStructureService(session).get_salary_structure(code, period)


def _applicable(view) -> dict[str, bool]:
    return {h.head_code: h.applicable for h in view.compliance}


def test_normal_employee(seeded_session):
    view = _view(seeded_session, "ARIF-112")
    (seg,) = view.segments
    assert seg.monthly_gross == Decimal(60000)
    assert seg.wage_bases["PF_WAGE"] == Decimal(30000)
    assert seg.template_check.is_consistent
    assert (seg.active_from, seg.active_to) == (SEP.start, SEP.end)
    applicable = _applicable(view)
    assert applicable["EE_PF"] and applicable["EPS"] and applicable["EPF_ADMIN"]
    assert not applicable["EE_ESIC"] and not applicable["ER_ESIC"]
    assert view.warnings == []


def test_lookup_is_case_insensitive(seeded_session):
    assert _view(seeded_session, " arif-112 ").employee_code == "ARIF-112"


def test_unknown_employee(seeded_session):
    with pytest.raises(NotFoundError):
        _view(seeded_session, "NOPE-1")


def test_all_seed_structures_match_templates(seeded_session):
    for employee in EmployeeRepository(seeded_session).list_all():
        for seg in _view(seeded_session, employee.employee_code).segments:
            assert seg.template_check.is_consistent, employee.employee_code


def test_increment_uses_structure_of_the_period(seeded_session):
    july = _view(seeded_session, "EMP-004", PayrollPeriod(2026, 7)).segments[0]
    aug = _view(seeded_session, "EMP-004", PayrollPeriod(2026, 8)).segments[0]
    assert (july.monthly_gross, july.reason) == (Decimal(50000), "REVISION")
    assert (aug.monthly_gross, aug.reason) == (Decimal(55000), "INCREMENT")


def test_employee_without_pf(seeded_session):
    applicable = _applicable(_view(seeded_session, "EMP-007"))
    for head in ("EE_PF", "ER_EPF", "EPS", "EDLI", "EPF_ADMIN"):
        assert applicable[head] is False
    assert applicable["TDS"] and applicable["PT"]


def test_esic_employee(seeded_session):
    applicable = _applicable(_view(seeded_session, "EMP-003"))
    assert applicable["EE_ESIC"] and applicable["ER_ESIC"]


def test_not_eps_eligible_reason(seeded_session):
    eps = next(h for h in _view(seeded_session, "EMP-009").compliance if h.head_code == "EPS")
    assert not eps.applicable
    assert eps.reason == "not applicable: eps_eligible = false"


def test_mid_month_joiner_and_leaver(seeded_session):
    joiner = _view(seeded_session, "EMP-005")
    assert joiner.segments[0].active_from == date(2026, 9, 15)
    assert "active only from 2026-09-15" in joiner.warnings[0]

    leaver = _view(seeded_session, "EMP-006")
    assert leaver.segments[0].active_to == date(2026, 9, 20)
    assert "active only until 2026-09-20" in leaver.warnings[0]


def test_not_employed_in_period(seeded_session):
    view = _view(seeded_session, "EMP-005", PayrollPeriod(2026, 8))
    assert view.segments == []
    assert "was not employed in 2026-08" in view.warnings[0]


def test_mid_month_revision_gives_two_segments(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("ARIF-112")
    old = employee.salary_assignments[0]
    old.effective_to = date(2026, 9, 15)
    new = EmployeeSalaryStructure(
        employee_id=employee.id, structure_id=old.structure_id, monthly_gross=Decimal(70000),
        effective_from=date(2026, 9, 16), reason=AssignmentReason.INCREMENT,
        components=[
            EmployeeSalaryComponent(head_code=h, monthly_amount=Decimal(a))
            for h, a in {"BASIC": 35000, "HRA": 17500, "CONVEYANCE": 1600, "MEDICAL": 1250,
                         "SPECIAL": 14650}.items()
        ],
    )  # fmt: skip
    seeded_session.add(new)
    seeded_session.flush()

    view = _view(seeded_session, "ARIF-112")
    assert [s.monthly_gross for s in view.segments] == [Decimal(60000), Decimal(70000)]
    assert view.segments[0].active_to == date(2026, 9, 15)
    assert view.segments[1].active_from == date(2026, 9, 16)
    assert any("Salary changed within 2026-09" in w for w in view.warnings)


def test_tampered_component_detected(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("EMP-003")
    basic = next(c for c in employee.salary_assignments[0].components if c.head_code == "BASIC")
    basic.monthly_amount = Decimal(9800)
    seeded_session.flush()

    view = _view(seeded_session, "EMP-003")
    check = view.segments[0].template_check
    assert not check.is_consistent
    assert any("components total 17000" in w for w in view.warnings)


def test_text_view(seeded_session):
    text = _view(seeded_session, "EMP-007").to_text()
    assert "EMPLOYER CONTRIBUTIONS (compliance)" in text
    assert "[NO ] Employee PF" in text
    assert "TEMPLATE CHECK: OK" in text
