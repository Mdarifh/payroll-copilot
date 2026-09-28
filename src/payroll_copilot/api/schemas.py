"""API request/response models. Money is always serialised as a string."""

from typing import Any

from pydantic import BaseModel, Field

from payroll_copilot.tools.schemas import EmployeeArgs, EmployeePeriodArgs, PeriodArgs, TDSArgs


class EmployeePeriodRequest(EmployeePeriodArgs):
    pass


class EmployeeRequest(EmployeeArgs):
    pass


class PeriodRequest(PeriodArgs):
    pass


class TDSRequest(TDSArgs):
    pass


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str = Field(default="api", min_length=1, max_length=100, pattern=r"^[\w.-]+$")


class ApprovalRequest(BaseModel):
    action_type: str = Field(description="TDS_OVERRIDE | ATTENDANCE_CORRECTION | SAVE_QA_RUN | "
                             "VERIFY_RULE")  # fmt: skip
    payload: dict[str, Any]
    reason: str | None = Field(default=None, max_length=500)


class ApprovalDecision(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)


class OperationResponse(BaseModel):
    """Result of an engine operation: readable text + structured data with traces."""

    text: str
    data: dict[str, Any]


class ErrorResponse(BaseModel):
    error: str
    detail: str
