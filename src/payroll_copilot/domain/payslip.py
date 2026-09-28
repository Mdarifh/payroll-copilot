"""Actual payslip (as produced by the payroll system under test)."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ActualPayslip(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    period: str
    paid_days: Decimal
    lop_days: Decimal
    earnings: dict[str, Decimal] = Field(default_factory=dict)  # per head, incl. arrears
    arrear_earnings: Decimal = Decimal(0)
    deductions: dict[str, Decimal] = Field(default_factory=dict)
    employer: dict[str, Decimal] = Field(default_factory=dict)
    gross_earnings: Decimal  # header values as stored
    total_deductions: Decimal
    net_pay: Decimal
    total_employer_contribution: Decimal
