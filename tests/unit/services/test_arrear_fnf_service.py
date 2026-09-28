from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError, NotFoundError
from payroll_copilot.db.models import Arrear, FnFSettlement, SalaryHead
from payroll_copilot.db.seed import seed_demo_data
from payroll_copilot.domain.enums import ArrearStatus, ComparisonStatus
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.fnf_service import FnFService
from payroll_copilot.services.payroll_service import PayrollCalculationService
from payroll_copilot.services.pt_service import PTService
from payroll_copilot.services.salary_service import SalaryService
from payroll_copilot.services.tds_service import TDSService

SEP, AUG = PayrollPeriod(2026, 9), PayrollPeriod(2026, 8)
D = Decimal


def _arrears(session):
    meena = EmployeeRepository(session).get_by_code("EMP-004")
    return list(session.scalars(select(Arrear).where(Arrear.employee_id == meena.id)))


class TestArrears:
    def test_august_is_calculated_as_paid(self, seeded_session):
        view = SalaryService(seeded_session).calculate("EMP-004", AUG)
        assert view.salary.gross_earnings == D(50000)
        assert view.arrears is None
        assert any("BASIC at old amount 25000" in w for w in view.warnings)

    def test_september_pays_the_arrear(self, seeded_session):
        view = SalaryService(seeded_session).calculate("EMP-004", SEP)
        assert view.salary.gross_earnings == D(60000)
        assert view.arrears.total == D(5000)
        assert view.salary.reduction["arrears"] == D(5000)
        assert view.arrears.months[0].pf_rule == "EPF v1"  # August rule, not September's
        assert set(view.arrears.pf_impact.values()) == {D(0)}

    def test_payslip_pf_not_inflated_by_arrear(self, seeded_session):
        view = PayrollCalculationService(seeded_session).calculate("EMP-004", SEP)
        pf = [d for d in view.summary.deductions if d.head_code == "EE_PF"]
        assert pf == [next(iter(pf))] and pf[0].amount == D(3000)
        assert view.pf.pf_wage_actual == D(27500)  # regular basic only

    def test_no_double_counting_in_half_year_pt(self, seeded_session):
        r = PTService(seeded_session).calculate("EMP-004", SEP)
        breakdown = r.trace.inputs["slab_wage_breakdown"]
        assert breakdown["2026-08 (calculated)"] == D(50000)
        assert r.slab_wage == D(310000)  # 4×50000 + 50000 (Aug as paid) + 60000 (Sep incl. arrear)

    def test_arrear_taxed_in_payment_month(self, seeded_session):
        r = TDSService(seeded_session).calculate("EMP-004", SEP)
        assert r.gross_taxable_salary == D(5 * 50000 + 60000 + 6 * 55000)

    def test_uploaded_amount_mismatch(self, seeded_session):
        basic = next(a for a in _arrears(seeded_session) if a.head_code == "BASIC")
        basic.amount = D(2000)
        seeded_session.flush()
        view = SalaryService(seeded_session).calculate("EMP-004", SEP)
        status = {c.head_code: c.status for c in view.arrears.comparisons}
        assert status["BASIC"] is ComparisonStatus.MISMATCH
        assert any("Uploaded arrear for BASIC is 2000, expected 2500" in w for w in view.warnings)

    def test_cancelled_arrear_is_ignored(self, seeded_session):
        for row in _arrears(seeded_session):
            row.status = ArrearStatus.CANCELLED
        seeded_session.flush()
        service = SalaryService(seeded_session)
        assert service.calculate("EMP-004", SEP).arrears is None
        assert service.calculate("EMP-004", AUG).salary.gross_earnings == D(55000)

    def test_arrear_must_be_for_a_past_month(self, seeded_session):
        for row in _arrears(seeded_session):
            row.paid_in_period = "2026-08"
        seeded_session.flush()
        with pytest.raises(CalculationError, match="not a past month"):
            SalaryService(seeded_session).calculate("EMP-004", AUG)

    def test_arrear_days_only_for_single_month(self, seeded_session):
        row = _arrears(seeded_session)[0]
        row.from_period, row.arrear_days = "2026-07", D(10)
        seeded_session.flush()
        with pytest.raises(CalculationError, match="split the upload per month"):
            SalaryService(seeded_session).calculate("EMP-004", SEP)


class TestFnF:
    def test_settlement(self, seeded_session):
        view = FnFService(seeded_session).calculate("EMP-006")
        s = view.final_payslip.summary
        assert view.fnf.leave_encashment == D(6000)
        assert s.gross_earnings == D(36000)
        assert view.final_payslip.pf.pf_wage_actual == D(15000)  # encashment not PF wage
        assert view.final_payslip.pt.slab_wage == D(30000)  # nor PT wage
        assert view.net_settlement == D(34000)
        assert "NET F&F SETTLEMENT: ₹34,000" in view.to_text()

    def test_notice_shortfall(self, seeded_session):
        employee = EmployeeRepository(seeded_session).get_by_code("EMP-006")
        settlement = seeded_session.scalar(
            select(FnFSettlement).where(FnFSettlement.employee_id == employee.id)
        )
        settlement.notice_days_served = 50
        seeded_session.flush()
        view = FnFService(seeded_session).calculate("EMP-006")
        assert view.fnf.notice_recovery == D(15000)  # 45000 / 30 × 10
        assert view.net_settlement == D(34000 - 15000)

    def test_missing_settlement(self, seeded_session):
        with pytest.raises(NotFoundError, match="No F&F settlement"):
            FnFService(seeded_session).calculate("ARIF-112")

    def test_lwd_mismatch(self, seeded_session):
        employee = EmployeeRepository(seeded_session).get_by_code("EMP-006")
        employee.lwd = employee.lwd.replace(day=19)
        seeded_session.flush()
        with pytest.raises(CalculationError, match="does not match"):
            FnFService(seeded_session).calculate("EMP-006")


def test_seed_adds_new_heads_to_existing_database(seeded_session):
    seeded_session.execute(delete(SalaryHead).where(SalaryHead.code == "NOTICE_RECOVERY"))
    report = seed_demo_data(seeded_session)
    assert report.skipped and report.counts == {"salary_heads": 1}
    assert seeded_session.scalar(select(SalaryHead).where(SalaryHead.code == "NOTICE_RECOVERY"))
