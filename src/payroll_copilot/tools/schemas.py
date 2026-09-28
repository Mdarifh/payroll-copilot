"""Validated argument schemas for the payroll tools."""

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PeriodError

_EMPLOYEE_CODE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,6}$")

KBModule = Literal["pf", "esic", "pt", "lwf", "tds", "payroll", "attendance", "arrears", "fnf",
                   "compliance"]  # fmt: skip


class EmployeeArgs(BaseModel):
    employee_code: str = Field(description="Employee code, e.g. ARIF-112 or EMP-002")

    @field_validator("employee_code")
    @classmethod
    def _code(cls, value: str) -> str:
        value = value.strip().upper()
        if not _EMPLOYEE_CODE.match(value):
            raise ValueError(f"'{value}' is not a valid employee code (expected like EMP-002)")
        return value


class PeriodArgs(BaseModel):
    period: str = Field(description="Payroll month in YYYY-MM format, e.g. 2026-09")

    @field_validator("period")
    @classmethod
    def _period(cls, value: str) -> str:
        try:
            return str(PayrollPeriod.parse(value))
        except PeriodError as exc:
            raise ValueError(str(exc)) from exc


class EmployeePeriodArgs(EmployeeArgs, PeriodArgs):
    """Employee code + payroll month."""


class TDSArgs(EmployeePeriodArgs):
    override_amount: Decimal | None = Field(
        default=None, ge=0,
        description="Optional what-if monthly TDS override (not saved anywhere)",
    )  # fmt: skip
    override_reason: str | None = Field(default=None, max_length=200)


class PolicySearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500, description="Policy question or keywords")
    module: KBModule | None = Field(default=None, description="Knowledge base module filter")
    period: str | None = Field(
        default=None, description="Only rules effective in this payroll month (YYYY-MM)"
    )
    state: str | None = Field(
        default=None, pattern=r"^[A-Z]{2,4}$", description="Work state code for PT/LWF, e.g. MH"
    )
    k: int = Field(default=4, ge=1, le=8, description="Number of passages")

    @field_validator("period")
    @classmethod
    def _period(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(PayrollPeriod.parse(value))
        except PeriodError as exc:
            raise ValueError(str(exc)) from exc
