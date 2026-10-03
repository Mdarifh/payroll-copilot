"""Request / response models of the API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    employee_id: int
    employee_code: str
    full_name: str
    email: str
    role: str
    department: str | None
    designation: str | None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserOut


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, pattern=r"^[\w-]{4,64}$")


class ChatResponse(BaseModel):
    conversation_id: str
    messages: list[dict[str, Any]]  # each has "type": text | payslip_card | ...
    tools_used: list[str]
    model: str | None
    warnings: list[str]
