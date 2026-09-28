"""Build the flat context that applicability conditions are evaluated against."""

from datetime import date
from typing import Any

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import AgeCategory


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def tax_age_category(dob: date, period: PayrollPeriod) -> AgeCategory:
    """Age category for the FY of ``period``, using age on the last day of that FY."""
    age = age_on(dob, period.financial_year.end)
    if age >= 80:
        return AgeCategory.SUPER_SENIOR
    if age >= 60:
        return AgeCategory.SENIOR
    return AgeCategory.GENERAL


def build_employee_context(employee: Employee, period: PayrollPeriod) -> dict[str, Any]:
    return {
        "employee_code": employee.employee_code,
        "gender": employee.gender.value,
        "work_state": employee.work_state,
        "pay_group": employee.pay_group,
        "doj": employee.doj,
        "lwd": employee.lwd,
        "age": age_on(employee.dob, period.end),
        "age_category": tax_age_category(employee.dob, period).value,
        "pf_applicable": employee.pf_applicable,
        "pf_join_date": employee.pf_join_date,
        "eps_eligible": employee.eps_eligible,
        "pf_wage_restricted": employee.pf_wage_restricted,
        "esic_applicable": employee.esic_applicable,
        "pt_applicable": employee.pt_applicable,
        "lwf_applicable": employee.lwf_applicable,
        "is_disabled": employee.is_disabled,
        "tax_regime": employee.tax_regime.value,
    }
