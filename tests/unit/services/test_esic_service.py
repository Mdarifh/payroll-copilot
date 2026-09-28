from datetime import date
from decimal import Decimal

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import EmployeeSalaryComponent, EmployeeSalaryStructure
from payroll_copilot.domain.enums import AssignmentReason
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.esic_service import ESICService, render_esic
from payroll_copilot.services.payroll_service import PayrollCalculationService

SEP = PayrollPeriod(2026, 9)


def test_esic_employees(seeded_session):
    service = ESICService(seeded_session)
    rohan = service.calculate("EMP-003", SEP)
    kavita = service.calculate("EMP-008", SEP)
    assert rohan.amounts == {"EE_ESIC": Decimal(135), "ER_ESIC": Decimal(585)}
    assert kavita.amounts == {"EE_ESIC": Decimal(144), "ER_ESIC": Decimal(622)}
    assert kavita.coverage_wage == Decimal(20500)  # full-month rate, not LOP-reduced wage


def test_non_esic_employee(seeded_session):
    r = ESICService(seeded_session).calculate("ARIF-112", SEP)
    assert not r.applicable
    assert "not applicable" in render_esic(r, "ARIF-112", "2026-09")


def test_increment_above_threshold_mid_period_stays_covered(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("EMP-008")
    old = employee.salary_assignments[0]
    old.effective_to = date(2026, 8, 31)
    seeded_session.add(
        EmployeeSalaryStructure(
            employee_id=employee.id, structure_id=old.structure_id, monthly_gross=Decimal(23000),
            effective_from=date(2026, 9, 1), reason=AssignmentReason.INCREMENT,
            components=[EmployeeSalaryComponent(head_code=h, monthly_amount=Decimal(a))
                        for h, a in {"BASIC": 13800, "HRA": 5520, "SPECIAL": 3680}.items()],
        )  # fmt: skip
    )
    seeded_session.flush()
    r = ESICService(seeded_session).calculate("EMP-008", SEP)
    assert r.covered and r.coverage_wage == Decimal(20500)
    assert any("stays covered until 2026-09-30" in w for w in r.warnings)


def test_disabled_employee_uses_higher_threshold(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("EMP-010")
    employee.esic_applicable = True
    seeded_session.flush()
    assert not ESICService(seeded_session).calculate("EMP-010", SEP).covered  # 24000 > 21000
    employee.is_disabled = True
    seeded_session.flush()
    assert ESICService(seeded_session).calculate("EMP-010", SEP).covered  # 24000 ≤ 25000


def test_payslip_with_pf_and_esic(seeded_session):
    view = PayrollCalculationService(seeded_session).calculate("EMP-003", SEP)
    s = view.summary
    assert [d.head_code for d in s.deductions] == ["EE_PF", "EE_ESIC", "PT", "TDS"]  # TDS ₹0
    assert s.net_pay == Decimal(18000 - 1296 - 135 - 130)  # PF, ESIC, WB PT
    assert s.total_employer_contributions == Decimal(396 + 900 + 54 + 54 + 585)
    assert s.monthly_ctc == Decimal(19989)
    assert "ESIC" not in view.pending_components
    assert "ESIC: not deducted" in PayrollCalculationService(seeded_session).calculate(
        "ARIF-112", SEP
    ).to_text()
