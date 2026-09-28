"""Calculation trace: the structured "show your work" returned by every calculation.

Every engine returns a ``CalculationResult`` holding the inputs, the rule used,
the formula, each intermediate step, raw and rounded values and warnings. The
LLM layer explains results only from this object, never from its own maths.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.core.money import RoundingMode, format_inr
from payroll_copilot.domain.enums import RuleStatus, RuleType


class TraceStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    formula: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    result: Decimal | None = None
    note: str | None = None
    unit: Literal["INR", "DAYS"] | None = None  # None => same unit as the result


class RuleRef(BaseModel):
    """Which rule version produced a value (for audit and explanations)."""

    model_config = ConfigDict(frozen=True)

    rule_type: RuleType | None = None
    rule_code: str
    version: int
    effective_from: date
    effective_to: date | None = None
    status: RuleStatus

    @classmethod
    def from_rule(cls, rule: Any, rule_type: RuleType | None = None) -> "RuleRef":
        return cls(
            rule_type=rule_type,
            rule_code=rule.rule_code,
            version=rule.version,
            effective_from=rule.effective_from,
            effective_to=rule.effective_to,
            status=rule.status,
        )

    @property
    def label(self) -> str:
        until = self.effective_to.isoformat() if self.effective_to else "open"
        return f"{self.rule_code} v{self.version} ({self.effective_from.isoformat()} to {until})"


class CalculationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    component: str
    unit: Literal["INR", "DAYS"] = "INR"
    employee_code: str | None = None
    period: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    rule: RuleRef | None = None
    formula: str
    steps: list[TraceStep] = Field(default_factory=list)
    raw_value: Decimal
    value: Decimal
    rounding_mode: RoundingMode = RoundingMode.NONE
    breakdown: dict[str, Decimal] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-safe dict (Decimals and dates as strings) for DB/API storage."""
        return self.model_dump(mode="json")

    def format_value(self, value: Decimal, unit: str | None = None) -> str:
        if (unit or self.unit) == "DAYS":
            return f"{value.normalize():f} days"
        return format_inr(value)

    def to_text(self) -> str:
        """Human-readable trace, used by the CLI and as LLM grounding."""
        fmt = self.format_value
        who = " | ".join(p for p in (self.employee_code, self.period) if p)
        lines = [f"{self.component}" + (f" | {who}" if who else "")]
        if self.rule:
            status = "" if self.rule.status is RuleStatus.VERIFIED else f" [{self.rule.status}]"
            lines.append(f"Rule: {self.rule.label}{status}")
        lines.append(f"Formula: {self.formula}")
        for number, step in enumerate(self.steps, start=1):
            text = f"{number}. {step.label}"
            if step.formula:
                text += f": {step.formula}"
            if step.result is not None:
                text += f" = {fmt(step.result, step.unit)}"
            if step.note:
                text += f"  ({step.note})"
            lines.append(text)
        result = f"Result: {fmt(self.value)}"
        if self.raw_value != self.value:
            raw = self.raw_value.quantize(Decimal("0.0001")).normalize()
            result += f" (raw {raw:f}, {self.rounding_mode})"
        lines.append(result)
        lines.extend(f"WARNING: {w}" for w in self.warnings)
        return "\n".join(lines)
